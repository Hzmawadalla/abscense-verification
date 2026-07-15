"""Credentials for the two access layers (SPEC §7).

TL layer: a long random bearer token in the link; only its SHA-256 hash is stored, compared in
constant time. A leaked token can at worst *excuse* an absence, and the default is absence-stands.

HRBP layer: per-user bcrypt password hashes (compatible with streamlit-authenticator)."""
import hashlib
import hmac
import os
import secrets

import bcrypt
from cryptography.fernet import Fernet, InvalidToken

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


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False
