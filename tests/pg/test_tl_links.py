"""TL link issuing and access against real PostgreSQL (Phase 3 PR A): legacy links unchanged until
the cutover, signed links stable until they expire, expired ones re-issued on re-send, revocation
by rotation, and access that never widens on failure. Nothing is sent."""
import datetime as dt
import logging
import socket

import pytest
from cryptography.fernet import Fernet

from app import data, security, tl_links

T0 = dt.datetime(2026, 10, 16, 9, 0, tzinfo=dt.timezone.utc)
DAY = dt.timedelta(days=1)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv(security.TOKEN_ENC_KEY_ENV, Fernet.generate_key().decode())
    real = socket.socket.connect

    def guarded(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        assert host in ("127.0.0.1", "::1", "localhost"), f"tried to reach {host!r}"
        return real(sock, address)
    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture()
def tls(conn):
    ids = {}
    with conn.cursor() as cur:
        for crm in ("TL-A", "TL-B"):
            cur.execute("insert into attendance.managers (crm, name) values (%s, %s) returning id",
                        (crm, crm))
            ids[crm] = cur.fetchone()[0]
    conn.commit()
    return ids


def _stored(conn, mid):
    with conn.cursor() as cur:
        cur.execute("select access_token_enc, access_token_hash from attendance.managers "
                    "where id = %s", (mid,))
        return cur.fetchone()


# ------------------------------------------------------------------ before the cutover
def test_before_the_cutover_links_stay_legacy_and_stable(conn, tls):
    assert tl_links.ISSUE_SIGNED_LINKS is False and tl_links.LEGACY_LINK_SUNSET is None
    tok, new = data.issue_manager_link(conn, tls["TL-A"], T0)
    assert new and security.check_link(tok, T0).kind == security.LINK_LEGACY
    again, new2 = data.issue_manager_link(conn, tls["TL-A"], T0 + 400 * DAY)
    assert (again, new2) == (tok, False)                         # legacy never "expires"
    access = data.tl_access(conn, tok, T0 + 400 * DAY)
    assert (access.status, access.scope) == ("ok", tl_links.SCOPE_CASE_ONLY)


def test_a_routine_resend_never_converts_a_legacy_link_before_the_cutover(conn, tls):
    legacy = data.rotate_manager_link(conn, tls["TL-A"], T0, signed=False)
    before = _stored(conn, tls["TL-A"])
    for _ in range(3):
        assert data.issue_manager_link(conn, tls["TL-A"], T0 + 90 * DAY) == (legacy, False)
    assert _stored(conn, tls["TL-A"]) == before                   # nothing rewritten


# ------------------------------------------------------------------ signed links
def test_a_signed_link_opens_team_scope_and_is_stable_until_it_expires(conn, tls):
    tok = data.rotate_manager_link(conn, tls["TL-A"], T0, signed=True)
    access = data.tl_access(conn, tok, T0 + 60 * DAY)
    assert (access.status, access.manager["crm"], access.scope) == ("ok", "TL-A",
                                                                     tl_links.SCOPE_TEAM)
    assert data.issue_manager_link(conn, tls["TL-A"], T0 + 60 * DAY) == (tok, False)


def test_an_expired_signed_link_is_reissued_on_resend_and_the_old_one_stops(conn, tls):
    old = data.rotate_manager_link(conn, tls["TL-A"], T0, signed=True)
    later = T0 + 60 * DAY + dt.timedelta(seconds=1)
    assert data.tl_access(conn, old, later).status == "expired"
    new, issued = data.issue_manager_link(conn, tls["TL-A"], later)     # legacy mode, still signed
    assert issued and new != old
    assert security.check_link(new, later).kind == security.LINK_SIGNED
    assert data.tl_access(conn, new, later).manager["crm"] == "TL-A"
    assert data.tl_access(conn, old, T0 + DAY).status == "invalid"      # revoked by the reissue


def test_after_the_cutover_a_resend_replaces_a_legacy_link_with_a_signed_one(conn, tls):
    legacy = data.rotate_manager_link(conn, tls["TL-A"], T0, signed=False)
    new, issued = data.issue_manager_link(conn, tls["TL-A"], T0, signed=True)
    assert issued and security.check_link(new, T0).kind == security.LINK_SIGNED
    assert data.tl_access(conn, legacy, T0).status == "invalid"
    assert data.issue_manager_link(conn, tls["TL-A"], T0, signed=True) == (new, False)


def test_rotation_revokes_a_signed_link(conn, tls):
    old = data.rotate_manager_link(conn, tls["TL-A"], T0, signed=True)
    new = data.rotate_manager_link(conn, tls["TL-A"], T0, signed=True)
    assert data.tl_access(conn, old, T0).status == "invalid"
    assert data.tl_access(conn, new, T0).status == "ok"


# ------------------------------------------------------------------ access never widens
def test_each_link_opens_only_its_own_tl(conn, tls):
    a = data.rotate_manager_link(conn, tls["TL-A"], T0, signed=True)
    b = data.rotate_manager_link(conn, tls["TL-B"], T0, signed=False)
    assert data.tl_access(conn, a, T0).manager["id"] == tls["TL-A"]
    assert data.tl_access(conn, b, T0).manager["id"] == tls["TL-B"]
    assert data.tl_access(conn, b, T0).scope == tl_links.SCOPE_CASE_ONLY


def test_a_correctly_signed_link_that_was_never_stored_is_invalid(conn, tls):
    assert data.tl_access(conn, security.mint_signed_token(T0), T0).status == "invalid"


def test_an_inactive_tl_link_is_invalid(conn, tls):
    tok = data.rotate_manager_link(conn, tls["TL-A"], T0, signed=True)
    with conn.cursor() as cur:
        cur.execute("update attendance.managers set active = false where id = %s", (tls["TL-A"],))
    conn.commit()
    assert data.tl_access(conn, tok, T0) == data.TLAccess("invalid")


def test_the_legacy_sunset_expires_legacy_links_only(conn, tls, monkeypatch):
    legacy = data.rotate_manager_link(conn, tls["TL-A"], T0, signed=False)
    signed = data.rotate_manager_link(conn, tls["TL-B"], T0, signed=True)
    monkeypatch.setattr(tl_links, "LEGACY_LINK_SUNSET", T0 + 10 * DAY)
    assert data.tl_access(conn, legacy, T0 + 9 * DAY).status == "ok"
    assert data.tl_access(conn, legacy, T0 + 10 * DAY).status == "expired"
    assert data.tl_access(conn, signed, T0 + 10 * DAY).status == "ok"


def test_failed_checks_never_reach_the_database_or_the_logs(conn, tls, caplog):
    tok = data.rotate_manager_link(conn, tls["TL-A"], T0, signed=True)
    caplog.set_level(logging.DEBUG)

    class NoDB:
        def cursor(self, **kw):
            raise AssertionError("an invalid or expired link must not query the database")
    v, rand, iat, sig = tok.split(".")
    for bad, status in [(tok, "expired"), (f"{v}.{rand}.{int(iat) + 9}.{sig}", "invalid"),
                        ("v1.garbage", "invalid"), ("", "invalid")]:
        assert data.tl_access(NoDB(), bad, T0 + 61 * DAY).status == status
    assert not any(rand in r.getMessage() for r in caplog.records)
