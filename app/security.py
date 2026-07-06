"""Credentials for the two access layers (SPEC §7).

TL layer: a long random bearer token in the link; only its SHA-256 hash is stored, compared in
constant time. A leaked token can at worst *excuse* an absence, and the default is absence-stands.

HRBP layer: per-user bcrypt password hashes (compatible with streamlit-authenticator)."""
import hashlib
import hmac
import secrets

import bcrypt

TOKEN_BYTES = 32


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
