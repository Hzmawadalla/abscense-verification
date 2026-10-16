"""Signed TL links (app.security, v1): signature, 60-day expiry boundary, tampering, malformed
input and the derived signing key. Pure — no database, no network."""
import base64
import datetime as dt

import pytest
from cryptography.fernet import Fernet

from app import security as s

T0 = dt.datetime(2026, 10, 16, 12, 0, 0, tzinfo=dt.timezone.utc)
DAY = dt.timedelta(days=1)


@pytest.fixture(autouse=True)
def key(monkeypatch):
    k = Fernet.generate_key().decode()
    monkeypatch.setenv(s.TOKEN_ENC_KEY_ENV, k)
    return k


def _parts(tok):
    v, rand, iat, sig = tok.split(".")
    return v, rand, iat, sig


def _resign(rand, iat):
    body = f"v1.{rand}.{iat}"
    return f"{body}.{s._signature(s._signing_key(), body)}"


# ------------------------------------------------------------------ valid + expiry boundary
def test_a_fresh_link_is_signed_and_carries_its_issue_time():
    tok = s.mint_signed_token(T0)
    c = s.check_link(tok, T0)
    assert c.kind == s.LINK_SIGNED
    assert (c.issued_at, c.expires_at) == (T0, T0 + 60 * DAY)
    assert len(_parts(tok)[1]) == 43                     # same 32-byte random part as before


def test_valid_through_exactly_60_days_and_expired_one_second_later():
    tok = s.mint_signed_token(T0)
    assert s.check_link(tok, T0 + 60 * DAY).kind == s.LINK_SIGNED
    assert s.check_link(tok, T0 + 60 * DAY + dt.timedelta(seconds=1)).kind == s.LINK_EXPIRED
    assert s.check_link(tok, T0 + 365 * DAY).kind == s.LINK_EXPIRED


def test_expiry_is_evaluated_in_utc_whatever_the_callers_timezone():
    cairo = dt.timezone(dt.timedelta(hours=3))
    tok = s.mint_signed_token(T0)
    assert s.check_link(tok, (T0 + 60 * DAY).astimezone(cairo)).kind == s.LINK_SIGNED
    with pytest.raises(ValueError):
        s.check_link(tok, dt.datetime(2026, 10, 16, 12))     # naive: refused, never guessed


# ------------------------------------------------------------------ tampering
def test_moving_the_issue_time_breaks_the_signature():
    v, rand, iat, sig = _parts(s.mint_signed_token(T0))
    for new_iat in (int(iat) + 1, int(iat) + 30 * 86400, int(iat) - 1):
        assert s.check_link(f"{v}.{rand}.{new_iat}.{sig}", T0).kind == s.LINK_INVALID


def test_tampered_signature_or_random_part_is_invalid():
    v, rand, iat, sig = _parts(s.mint_signed_token(T0))
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    assert s.check_link(f"{v}.{rand}.{iat}.{flipped}", T0).kind == s.LINK_INVALID
    other = ("A" if rand[0] != "A" else "B") + rand[1:]
    assert s.check_link(f"{v}.{other}.{iat}.{sig}", T0).kind == s.LINK_INVALID


def test_a_link_signed_with_another_key_is_invalid(monkeypatch):
    tok = s.mint_signed_token(T0)
    monkeypatch.setenv(s.TOKEN_ENC_KEY_ENV, Fernet.generate_key().decode())
    assert s.check_link(tok, T0).kind == s.LINK_INVALID


def test_an_issue_time_in_the_future_is_rejected_even_if_correctly_signed():
    rand = _parts(s.mint_signed_token(T0))[1]
    far = int((T0 + dt.timedelta(minutes=6)).timestamp())
    near = int((T0 + dt.timedelta(minutes=4)).timestamp())
    assert s.check_link(_resign(rand, far), T0).kind == s.LINK_INVALID      # can't extend life
    assert s.check_link(_resign(rand, near), T0).kind == s.LINK_SIGNED      # small clock skew


# ------------------------------------------------------------------ malformed input
@pytest.mark.parametrize("tok", [
    None, "", " ", "v1", "v1.", "v1...", "v1.a.b.c", "v2.x.1.y", 12345, b"bytes",
    "short", "has a space in it ok", "dot.ted-legacy-token-xyz", "x" * 129,
    "ünïcödé-ünïcödé-ünïcödé",
])
def test_malformed_tokens_are_invalid_and_never_raise(tok):
    assert s.check_link(tok, T0).kind == s.LINK_INVALID


def test_a_trailing_newline_or_extra_segment_is_invalid():
    tok = s.mint_signed_token(T0)
    assert s.check_link(tok + "\n", T0).kind == s.LINK_INVALID
    assert s.check_link(tok + ".x", T0).kind == s.LINK_INVALID
    assert s.check_link("v1." + tok, T0).kind == s.LINK_INVALID


def test_legacy_tokens_are_recognised_as_legacy():
    assert s.check_link(s.generate_token(), T0).kind == s.LINK_LEGACY
    assert s.check_link("test-token-not-a-secret", T0).kind == s.LINK_LEGACY


# ------------------------------------------------------------------ the signing key
def test_without_a_key_signed_links_fail_closed_but_legacy_still_classifies(monkeypatch):
    tok = s.mint_signed_token(T0)
    monkeypatch.delenv(s.TOKEN_ENC_KEY_ENV)
    assert s.check_link(tok, T0).kind == s.LINK_INVALID
    assert s.check_link(s.generate_token(), T0).kind == s.LINK_LEGACY
    with pytest.raises(RuntimeError) as e:
        s.mint_signed_token(T0)
    assert "v1." not in str(e.value)


def test_a_malformed_key_fails_closed(monkeypatch):
    tok = s.mint_signed_token(T0)
    for bad in ("not-base64!!", base64.urlsafe_b64encode(b"short").decode()):
        monkeypatch.setenv(s.TOKEN_ENC_KEY_ENV, bad)
        assert s._signing_key() is None
        assert s.check_link(tok, T0).kind == s.LINK_INVALID


def test_the_signing_key_is_derived_never_the_encryption_key_itself(key):
    raw = base64.urlsafe_b64decode(key)
    derived = s._signing_key()
    assert len(derived) == 32 and derived != raw
    assert derived not in (raw[:16] * 2, raw[16:] * 2)
    assert s._signing_key() == derived                    # deterministic for the same key


def test_an_out_of_range_but_validly_signed_issue_time_is_invalid_not_a_crash():
    rand = _parts(s.mint_signed_token(T0))[1]
    for iat in (999_999_999_999, 253_402_300_799):            # past year 9999 / at its edge
        assert s.check_link(_resign(rand, iat), T0).kind == s.LINK_INVALID


def test_the_link_policy_refuses_an_inconsistent_cutover():
    from app import tl_links
    tl_links.validate_policy(False, None)
    tl_links.validate_policy(True, None)
    tl_links.validate_policy(True, T0)
    with pytest.raises(ValueError):
        tl_links.validate_policy(True, dt.datetime(2026, 11, 1))          # naive
    with pytest.raises(ValueError):
        tl_links.validate_policy(False, T0)                               # sunset before signing
    assert (tl_links.ISSUE_SIGNED_LINKS, tl_links.LEGACY_LINK_SUNSET) == (False, None)
