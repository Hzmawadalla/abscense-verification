"""Credentials for the two access layers (SPEC §7).

TL layer: a long random bearer token in the link; only its SHA-256 hash is stored, compared in
constant time. A leaked token can at worst *excuse* an absence, and the default is absence-stands.

Signed TL links (Phase 3, v1): `v1.<random>.<issued_at>.<signature>`. <random> is the same 32-byte
random token as before; <issued_at> is whole UTC seconds since the epoch; <signature> is
HMAC-SHA256 over `v1.<random>.<issued_at>` with a key DERIVED from TOKEN_ENC_KEY by HKDF-SHA256
(fixed, versioned label) — TOKEN_ENC_KEY itself is never used to sign. A link is valid until
issued_at + exactly 60 x 24 h and expired after that. The issue time can't be changed without
breaking the signature, and the signature is checked before any database lookup. Older links
without that shape are "legacy": app.tl_links decides what they may still do.

HRBP layer: per-user bcrypt password hashes (compatible with streamlit-authenticator)."""
import base64
import datetime as dt
import hashlib
import hmac
import os
import re
import secrets
from typing import NamedTuple

import bcrypt
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

TOKEN_BYTES = 32
TOKEN_ENC_KEY_ENV = "TOKEN_ENC_KEY"


def _fernet() -> Fernet:
    """Fernet built from TOKEN_ENC_KEY (a urlsafe-base64 32-byte key held in app secrets / env,
    never in the database). Raising here surfaces a misconfiguration rather than storing plaintext."""
    key = os.environ.get(TOKEN_ENC_KEY_ENV)
    if not key:
        raise RuntimeError(
            f"{TOKEN_ENC_KEY_ENV} is not configured — TL link encryption is unavailable. "
            "Generate one with Fernet.generate_key() and set it in Streamlit Secrets / env.")
    return Fernet(key.encode("utf-8") if isinstance(key, str) else key)


def encrypt_token(token: str) -> str:
    """Encrypt a raw TL token for at-rest storage."""
    return _fernet().encrypt(token.encode("utf-8")).decode("utf-8")


def decrypt_token(ciphertext: str) -> str | None:
    """Recover a raw TL token from stored ciphertext, or None if unreadable (e.g. the key was
    rotated) — the caller then mints a fresh token instead."""
    if not ciphertext:
        return None
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def generate_token() -> str:
    """A fresh, URL-safe TL access token (the raw value only ever lives in the emailed link)."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token: str, token_hash: str) -> bool:
    if not token or not token_hash:
        return False
    return hmac.compare_digest(hash_token(token), token_hash)


# --------------------------------------------------------------------------- signed TL links
SIGNED_LINK_VERSION = "v1"
LINK_LIFETIME = dt.timedelta(days=60)            # valid until issued_at + 60 x 24 h, inclusive
MAX_CLOCK_SKEW = dt.timedelta(minutes=5)          # an issue time further in the future is forged
_HKDF_INFO = b"attendance-verification/tl-link-signing/v1"
_B64 = r"[A-Za-z0-9_-]"
_SIGNED_RE = re.compile(rf"v1\.({_B64}{{43}})\.([0-9]{{1,12}})\.({_B64}{{43}})")
_LEGACY_RE = re.compile(rf"{_B64}{{16,128}}")   # pre-v1 links: token_urlsafe, no dots

LINK_SIGNED, LINK_LEGACY, LINK_EXPIRED, LINK_INVALID = "signed", "legacy", "expired", "invalid"


class LinkCheck(NamedTuple):
    kind: str                         # LINK_SIGNED / LINK_LEGACY / LINK_EXPIRED / LINK_INVALID
    issued_at: dt.datetime | None = None
    expires_at: dt.datetime | None = None


def _utc(now) -> dt.datetime:
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError("naive datetime: pass an aware UTC time")
    return now.astimezone(dt.timezone.utc)


def _signing_key() -> bytes | None:
    """A dedicated HMAC key derived from TOKEN_ENC_KEY (HKDF-SHA256, versioned label). None when
    TOKEN_ENC_KEY is missing or malformed: signed links then fail closed (never verify)."""
    raw = os.environ.get(TOKEN_ENC_KEY_ENV)
    if not raw:
        return None
    try:
        ikm = base64.urlsafe_b64decode(raw.encode("utf-8") if isinstance(raw, str) else raw)
    except (ValueError, TypeError):
        return None
    if len(ikm) != 32:
        return None
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(ikm)


def _signature(key: bytes, body: str) -> str:
    mac = hmac.new(key, body.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def mint_signed_token(now=None) -> str:
    """A fresh v1 signed link token issued at `now` (UTC). Raises if no signing key is available."""
    key = _signing_key()
    if key is None:
        raise RuntimeError(f"{TOKEN_ENC_KEY_ENV} is not configured — signed TL links are unavailable.")
    body = f"{SIGNED_LINK_VERSION}.{generate_token()}.{int(_utc(now).timestamp())}"
    return f"{body}.{_signature(key, body)}"


def check_link(token, now=None) -> LinkCheck:
    """Classify a raw link token WITHOUT touching the database. Signature first, then expiry; any
    malformed, tampered or unverifiable token is LINK_INVALID. The database lookup that follows
    (hash match = not revoked) is the caller's job."""
    if not isinstance(token, str) or not token:
        return LinkCheck(LINK_INVALID)
    m = _SIGNED_RE.fullmatch(token)     # fullmatch: no trailing newline slips through
    if m:
        key = _signing_key()
        if key is None:
            return LinkCheck(LINK_INVALID)
        body = token.rsplit(".", 1)[0]
        if not hmac.compare_digest(_signature(key, body), m.group(3)):
            return LinkCheck(LINK_INVALID)
        issued = dt.datetime.fromtimestamp(int(m.group(2)), dt.timezone.utc)
        expires = issued + LINK_LIFETIME
        now = _utc(now)
        if issued > now + MAX_CLOCK_SKEW:
            return LinkCheck(LINK_INVALID)
        if now > expires:
            return LinkCheck(LINK_EXPIRED, issued, expires)
        return LinkCheck(LINK_SIGNED, issued, expires)
    if token.startswith(SIGNED_LINK_VERSION + ".") or not _LEGACY_RE.fullmatch(token):
        return LinkCheck(LINK_INVALID)
    return LinkCheck(LINK_LEGACY)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False
