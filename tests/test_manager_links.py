"""Behavior contract for TL link tokens: generation is idempotent; rotation is explicit."""
from app import data, security


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
        if s.startswith("select access_token from attendance.managers"):
            row = self.store.get(params[0])
            self._fetch = (row.get("access_token"),) if row else None
        elif s.startswith("update attendance.managers set access_token"):
            token, token_hash, mid = params
            self.store.setdefault(mid, {}).update(access_token=token, access_token_hash=token_hash)
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
    return {"access_token": None, "access_token_hash": None, "active": True}


def test_generate_mints_and_stores_a_token_when_none_exists():
    conn = FakeConn({"m1": _mgr()})
    tok = data.generate_manager_link(conn, "m1")
    assert tok
    assert conn.store["m1"]["access_token"] == tok
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
