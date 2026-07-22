"""Behavior contract for HRBP upload management: list, remove-one, reset-all."""
from app import data


class FakeCursor:
    """psycopg-like cursor over an in-memory {run_id: [case,...]} store, where a case is a dict
    {'verified': bool, 'open': bool}. Only the queries these functions issue are modelled."""

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
            self._all = [
                {"id": rid, "source_filename": f"{rid}.xlsx", "created_at": None,
                 "total": len(cs),
                 "verified": sum(1 for x in cs if x["verified"]),
                 "open": sum(1 for x in cs if x["open"])}
                for rid, cs in self.store.items()]
        elif s.startswith("select count(*), count(*) filter") and "ingestion_run_id = %s" in s:
            cs = self.store.get(params[0], [])
            self._one = (len(cs), sum(1 for x in cs if x["verified"]))
        elif s.startswith("delete from attendance.cases where ingestion_run_id = %s"):
            self.store.pop(params[0], None)
        elif s.startswith("delete from attendance.ingestion_runs where id = %s"):
            pass
        elif s.startswith("select count(*) from attendance.cases"):
            self._one = (sum(len(cs) for cs in self.store.values()),)
        elif s.startswith("delete from attendance.cases"):
            self.store.clear()
        elif s.startswith("delete from attendance.ingestion_exceptions"):
            pass
        elif s.startswith("delete from attendance.ingestion_runs"):
            pass

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class FakeConn:
    def __init__(self, store):
        self.store, self.calls, self.committed = store, [], 0

    def cursor(self, row_factory=None):
        return FakeCursor(self.store, self.calls)

    def commit(self):
        self.committed += 1


def _store():
    return {
        "A": [{"verified": True, "open": False}, {"verified": False, "open": True}],
        "B": [{"verified": False, "open": True}, {"verified": False, "open": True},
              {"verified": True, "open": False}],
    }


def test_list_uploads_reports_counts_per_run():
    conn = FakeConn(_store())
    rows = {r["id"]: r for r in data.list_uploads(conn)}
    assert rows["A"]["total"] == 2 and rows["A"]["verified"] == 1 and rows["A"]["open"] == 1
    assert rows["B"]["total"] == 3 and rows["B"]["verified"] == 1


def test_remove_upload_deletes_only_that_run_and_reports_counts():
    conn = FakeConn(_store())
    res = data.remove_upload(conn, "A")
    assert res == {"cases_deleted": 2, "verified_deleted": 1}
    assert "A" not in conn.store          # A's cases gone
    assert len(conn.store["B"]) == 3      # B untouched
    assert conn.committed == 1


def test_reset_all_cases_clears_everything_but_not_managers():
    conn = FakeConn(_store())
    res = data.reset_all_cases(conn)
    assert res == {"cases_deleted": 5}
    assert conn.store == {}
    joined = " | ".join(conn.calls)
    assert "delete from attendance.cases" in joined
    assert "delete from attendance.ingestion_exceptions" in joined
    assert "delete from attendance.ingestion_runs" in joined
    assert "managers" not in joined       # reference data is never touched
