"""Behavior contract for TL link tokens: generation is idempotent, the stored token is encrypted
at rest, and rotation is explicit."""
import os

from cryptography.fernet import Fernet

# A key must exist before security._fernet() runs; a per-run key round-trips fine within the suite.
os.environ.setdefault("TOKEN_ENC_KEY", Fernet.generate_key().decode())

from app import data, security  # noqa: E402 — import after the key is set


class FakeCursor:
    """psycopg-like cursor over an in-memory {manager_id: row} store."""

    def __init__(self, store, calls):
        self.store, self.calls = store, calls
        self._fetch = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.lower().split())
        self.calls.append(s.split()[0])  # 'select' / 'update'
        if s.startswith("select access_token_enc from attendance.managers"):
            row = self.store.get(params[0])
            self._fetch = (row.get("access_token_enc"),) if row else None
        elif s.startswith("update attendance.managers set access_token_enc"):
            enc, token_hash, mid = params
            self.store.setdefault(mid, {}).update(access_token_enc=enc, access_token_hash=token_hash)
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
    def __init__(self, store):
        self.store, self.calls, self.committed = store, [], 0

    def cursor(self, row_factory=None):
        return FakeCursor(self.store, self.calls)

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
