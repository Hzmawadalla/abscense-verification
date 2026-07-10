"""Behavior contract for submit_verdict — a case may be validated by the TL exactly once."""
from app import data


class FakeCursor:
    """Minimal psycopg-like cursor over an in-memory {case_id: row} store."""

    def __init__(self, store, calls):
        self.store, self.calls = store, calls
        self._fetch = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.lower().split())
        self.calls.append(s.split()[0])  # 'select' / 'update' / 'insert'
        if s.startswith("select status"):
            row = self.store.get(params[0])
            self._fetch = dict(row) if row else None          # snapshot, like a real fetch
        elif s.startswith("update attendance.cases"):
            row = self.store.get(params[-1])                  # last param is the case id
            if row and row["status"] == "open":               # the one-time guard
                row.update(status="manager_responded", manager_status=params[0],
                           leave_type=params[1], manager_comment=params[2])
                self.rowcount = 1
            else:
                self.rowcount = 0
        # audit-log inserts are ignored by the fake

    def fetchone(self):
        return self._fetch


class FakeConn:
    def __init__(self, store):
        self.store, self.calls = store, []
        self.committed = self.rolledback = 0

    def cursor(self, row_factory=None):
        return FakeCursor(self.store, self.calls)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolledback += 1


def _case(status="open"):
    return {"status": status, "manager_status": None, "leave_type": None, "manager_comment": None}


def test_first_submit_on_open_case_succeeds():
    conn = FakeConn({1: _case("open")})
    assert data.submit_verdict(conn, 1, "annual_leave", None, "approved", "tl:TL-A") is True
    assert conn.store[1]["status"] == "manager_responded"
    assert conn.store[1]["manager_status"] == "annual_leave"


def test_second_submit_on_same_case_is_rejected_and_leaves_first_answer():
    conn = FakeConn({1: _case("open")})
    assert data.submit_verdict(conn, 1, "annual_leave", None, "first", "tl:TL-A") is True
    # TL tries again with a different verdict — must be refused, original preserved
    assert data.submit_verdict(conn, 1, "absent", None, "second", "tl:TL-A") is False
    assert conn.store[1]["manager_status"] == "annual_leave"
    assert conn.store[1]["manager_comment"] == "first"


def test_submit_on_closed_case_is_rejected():
    conn = FakeConn({1: _case("closed")})
    assert data.submit_verdict(conn, 1, "present", None, "", "tl:TL-A") is False


def test_submit_on_missing_case_is_rejected():
    conn = FakeConn({})
    assert data.submit_verdict(conn, 999, "present", None, "", "tl:TL-A") is False
