"""Behavior contract for HRBP upload management: list, remove-one, reset-all.

Phase 0: an upload (or the whole case table) can only be deleted while none of its cases has
history — a live answer, a non-open state, any attachment or any audit entry. A voided case has
no live answer but does have history, so it must block deletion too.
"""
from app import data


class FakeCursor:
    """psycopg-like cursor over an in-memory {run_id: [case,...]} store, where a case is a dict
    {'verified': bool, 'open': bool, 'history': bool}. Only the queries these functions issue
    are modelled; the history predicate itself is SQL and is exercised in tests/pg/."""

    def __init__(self, store, calls):
        self.store, self.calls = store, calls
        self._one = None
        self._all = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.lower().split())
        self.calls.append(s)
        if s.startswith("select r.id, r.source_filename"):
            assert "as protected" in s
            self._all = [
                {"id": rid, "source_filename": f"{rid}.xlsx", "created_at": None,
                 "total": len(cs),
                 "verified": sum(1 for x in cs if x["verified"]),
                 "open": sum(1 for x in cs if x["open"]),
                 "protected": sum(1 for x in cs if x["history"])}
                for rid, cs in self.store.items()]
        elif s.startswith("select count(*) from attendance.cases c where c.ingestion_run_id = %s"):
            assert "attendance.audit_log" in s and "attendance.case_attachments" in s
            self._one = (sum(1 for x in self.store.get(params[0], []) if x["history"]),)
        elif s.startswith("select count(*) from attendance.cases c where true"):
            self._one = (sum(1 for cs in self.store.values() for x in cs if x["history"]),)
        elif s.startswith("select count(*) from attendance.cases where ingestion_run_id = %s"):
            self._one = (len(self.store.get(params[0], [])),)
        elif s.startswith("delete from attendance.cases where ingestion_run_id = %s"):
            self.store.pop(params[0], None)
        elif s.startswith("select count(*) from attendance.cases"):
            self._one = (sum(len(cs) for cs in self.store.values()),)
        elif s.startswith("delete from attendance.cases"):
            self.store.clear()

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class FakeConn:
    def __init__(self, store):
        self.store, self.calls, self.committed, self.rolled_back = store, [], 0, 0

    def cursor(self, row_factory=None):
        return FakeCursor(self.store, self.calls)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


def _c(verified=False, open_=True, history=False):
    return {"verified": verified, "open": open_, "history": history}


def _store():
    return {
        "A": [_c(verified=True, open_=False, history=True), _c()],
        "B": [_c(), _c(), _c(verified=True, open_=False, history=True)],
        "C": [_c(), _c()],                                       # never touched
        "V": [_c(history=True)],    # voided: open again, no live answer, but has audit history
    }


def _deletes(conn):
    return [q for q in conn.calls if q.startswith("delete")]


def test_list_uploads_reports_counts_per_run():
    conn = FakeConn(_store())
    rows = {r["id"]: r for r in data.list_uploads(conn)}
    assert rows["A"]["total"] == 2 and rows["A"]["verified"] == 1 and rows["A"]["open"] == 1
    assert rows["B"]["total"] == 3 and rows["B"]["verified"] == 1
    assert rows["A"]["protected"] == 1 and rows["C"]["protected"] == 0
    assert rows["V"]["protected"] == 1 and rows["V"]["verified"] == 0


def test_remove_untouched_upload_deletes_only_that_run():
    conn = FakeConn(_store())
    res = data.remove_upload(conn, "C")
    assert res == {"cases_deleted": 2}
    assert "C" not in conn.store
    assert len(conn.store["B"]) == 3      # other runs untouched
    assert conn.committed == 1


def test_remove_upload_with_answered_case_is_refused():
    conn = FakeConn(_store())
    res = data.remove_upload(conn, "A")
    assert res == {"refused": 1}
    assert len(conn.store["A"]) == 2
    assert _deletes(conn) == []
    assert conn.committed == 0 and conn.rolled_back == 1


def test_remove_upload_with_voided_case_is_refused():
    # The case has no live answer (it was voided and reopened) but still has history.
    conn = FakeConn(_store())
    res = data.remove_upload(conn, "V")
    assert res == {"refused": 1}
    assert "V" in conn.store
    assert _deletes(conn) == []


def test_reset_all_cases_refused_while_any_case_has_history():
    conn = FakeConn(_store())
    res = data.reset_all_cases(conn)
    assert res == {"refused": 3}
    assert len(conn.store) == 4
    assert _deletes(conn) == []


def test_reset_all_cases_clears_everything_but_not_managers_when_untouched():
    conn = FakeConn({"C": [_c(), _c()], "D": [_c()]})
    res = data.reset_all_cases(conn)
    assert res == {"cases_deleted": 3}
    assert conn.store == {}
    joined = " | ".join(conn.calls)
    assert "delete from attendance.cases" in joined
    assert "delete from attendance.ingestion_exceptions" in joined
    assert "delete from attendance.ingestion_runs" in joined
    assert "managers" not in joined       # reference data is never touched
