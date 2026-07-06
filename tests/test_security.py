"""Contract for TL tokens and HRBP password hashing (SPEC §7)."""
from app import security


def test_token_roundtrip_verifies():
    tok = security.generate_token()
    h = security.hash_token(tok)
    assert security.verify_token(tok, h) is True


def test_wrong_token_rejected():
    h = security.hash_token(security.generate_token())
    assert security.verify_token(security.generate_token(), h) is False
    assert security.verify_token("", h) is False
    assert security.verify_token("x", "") is False


def test_tokens_are_unique_and_urlsafe():
    a, b = security.generate_token(), security.generate_token()
    assert a != b
    assert all(c.isalnum() or c in "-_" for c in a)


def test_password_roundtrip():
    h = security.hash_password("s3cret!")
    assert security.verify_password("s3cret!", h) is True
    assert security.verify_password("wrong", h) is False
    assert security.verify_password("", h) is False
