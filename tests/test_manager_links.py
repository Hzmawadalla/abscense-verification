"""Behavior contract for TL link tokens: generation is idempotent, the stored token is encrypted
at rest, and rotation is explicit."""
import os

from cryptography.fernet import Fernet

# A key must exist before security._fernet() runs; a per-run key round-trips fine within the suite.
os.environ.setdefault("TOKEN_ENC_KEY", Fernet.generate_key().decode())

from app import data, security  # noqa: E402 — import after the key is set


class FakeCursor:
    """psycopg-like cursor over an in-memory {manager_id: row} store."""

    def __init__(self, store, calls, conn_hook=None):
        self.store, self.calls, self.conn_hook = store, calls, conn_hook
        self._fetch = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.lower().split())
        self.calls.append(s.split()[0])  # 'select' / 'update'
        self.rowcount = 0
        if s.startswith("select access_token_enc, access_token_hash from attendance.managers"):
            row = self.store.get(params[0])
            self._fetch = (row.get("access_token_enc"), row.get("access_token_hash")) if row else None
            if self.conn_hook:                 # simulate another session writing in between
                self.conn_hook(self.store)
        elif s.startswith("update attendance.managers set access_token_enc"):
            # changed on purpose (Phase 3 PR A): the issue path is compare-and-set on the hash
            enc, token_hash, mid, *expected = params
            row = self.store.setdefault(mid, {})
            if expected and row.get("access_token_hash") != expected[0]:
                return
            row.update(access_token_enc=enc, access_token_hash=token_hash)
            self.rowcount = 1
        elif s.startswith("select id, crm, name, email, access_token_hash, active"):
            want = params[0]
            self._fetch = next(
                ({"id": mid, "crm": r.get("crm"), "name": r.get("name"), "email": r.get("email"),
                  "access_token_hash": r.get("access_token_hash"), "active": True}
                 for mid, r in self.store.items()
                 if r.get("access_token_hash") == want and r.get("active", True)),
                None)

    def fetchone(self):
        return self._fetch


class FakeConn:
    def __init__(self, store, hook=None):
        self.store, self.calls, self.committed, self.hook = store, [], 0, hook

    def cursor(self, row_factory=None):
        hook, self.hook = self.hook, None      # fire once
        return FakeCursor(self.store, self.calls, hook)

    def commit(self):
        self.committed += 1


def _mgr():
    return {"access_token_enc": None, "access_token_hash": None, "active": True}


def test_generate_mints_stores_encrypted_and_hashes():
    conn = FakeConn({"m1": _mgr()})
    tok = data.generate_manager_link(conn, "m1")
    stored = conn.store["m1"]["access_token_enc"]
    assert tok
    assert stored != tok                                   # stored value is ciphertext, not plaintext
    assert security.decrypt_token(stored) == tok           # ...that round-trips back to the token
    assert conn.store["m1"]["access_token_hash"] == security.hash_token(tok)


def test_generate_is_idempotent_returns_same_token_without_rewriting():
    conn = FakeConn({"m1": _mgr()})
    first = data.generate_manager_link(conn, "m1")
    updates_after_first = conn.calls.count("update")
    second = data.generate_manager_link(conn, "m1")
    assert second == first
    assert conn.calls.count("update") == updates_after_first  # repeat call performs no write


def test_rotate_replaces_token_and_invalidates_the_old_link():
    conn = FakeConn({"m1": _mgr()})
    old = data.generate_manager_link(conn, "m1")
    new = data.rotate_manager_link(conn, "m1")
    assert new != old
    assert data.manager_by_token(conn, old) is None
    assert data.manager_by_token(conn, new)["id"] == "m1"


def test_decrypt_token_returns_none_on_unreadable_ciphertext():
    # Resilience: garbage or a value encrypted under a different key is treated as "no token",
    # so generate_manager_link mints a fresh one rather than crashing.
    assert security.decrypt_token("not-a-valid-fernet-token") is None
    assert security.decrypt_token("") is None


def test_an_undecryptable_or_odd_stored_token_is_replaced_and_flagged_new():
    for stored in ("not-fernet", security.encrypt_token("short")):
        conn = FakeConn({"m1": {**_mgr(), "access_token_enc": stored, "access_token_hash": "h"}})
        tok, new = data.issue_manager_link(conn, "m1")
        assert new and security.check_link(tok).kind == security.LINK_LEGACY
        assert conn.store["m1"]["access_token_hash"] == security.hash_token(tok)


def test_a_concurrent_reissue_keeps_the_other_sessions_link():
    conn = FakeConn({"m1": _mgr()})
    theirs = security.generate_token()

    def other_session(store):
        store["m1"].update(access_token_enc=security.encrypt_token(theirs),
                           access_token_hash=security.hash_token(theirs))
    conn.hook = other_session
    tok, new = data.issue_manager_link(conn, "m1")
    assert (tok, new) == (theirs, False)                # ours lost the race and returns theirs
    assert conn.store["m1"]["access_token_hash"] == security.hash_token(theirs)
