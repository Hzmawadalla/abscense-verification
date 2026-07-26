"""Contract for voiding a TL's submissions: reopen closed-by-tl cases, purge attachments, audit.

Uses a purpose-built in-memory fake (same spirit as test_submit_verdict) so the reopen/skip/audit
logic is exercised without a database.
"""
from app import data


def _case(cid, *, manager_id=1, status="closed", closed_by="tl",
          manager_status="present", employee_name="Emp", employee_crm="CRM-1"):
    return {"id": cid, "manager_id": manager_id, "status": status, "closed_by": closed_by,
            "manager_status": manager_status, "final_status": manager_status,
            "leave_type": None, "manager_comment": "note", "work_date": "2026-07-06",
            "source_status": "Absent", "is_half_day": False,
            "employee_name": employee_name, "employee_crm": employee_crm}


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._fetch = None
        self._fetchall = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.lower().split())
        p = params or ()
        if s.startswith("select status, manager_status, final_status, leave_type, "
                        "manager_comment, closed_by"):
            row = self.db["cases"].get(p[0])
            self._fetch = dict(row) if row else None
        elif s.startswith("select storage_path from attendance.case_attachments"):
            self._fetchall = [{"storage_path": x} for x in self.db["att"].get(p[0], [])]
        elif s.startswith("delete from attendance.case_attachments"):
            self.db["att"][p[0]] = []
        elif s.startswith("update attendance.cases set status = 'open'"):
            row = self.db["cases"].get(p[0])
            if row and row["closed_by"] == "tl" and row["status"] == "closed":
                row.update(status="open", manager_status=None, final_status=None,
                           leave_type=None, manager_comment=None, closed_by=None)
                self.rowcount = 1
            else:
                self.rowcount = 0
        elif s.startswith("insert into attendance.audit_log"):
            self.db["audit"].append({"case_id": p[0], "actor": p[1], "action": p[2],
                                     "old": p[3], "new": p[4]})
        elif s.startswith("select c.id, c.work_date"):
            self._fetchall = [dict(r) for r in self.db["cases"].values()
                              if r["manager_id"] == p[0] and r["closed_by"] == "tl"
                              and r["status"] == "closed"]

    def fetchone(self):
        return self._fetch

    def fetchall(self):
        return self._fetchall


class FakeConn:
    def __init__(self, cases=None, att=None):
        self.db = {"cases": cases or {}, "att": att or {}, "audit": []}
        self.committed = 0

    def cursor(self, row_factory=None):
        return FakeCursor(self.db)

    def commit(self):
        self.committed += 1


def test_reopen_reverts_tl_case_and_writes_audit():
    conn = FakeConn({7: _case(7)})
    res = data.reopen_tl_cases(conn, [7], "hrbp:hazem", "wrong verdict")
    assert res["reopened"] == 1
    assert conn.db["cases"][7]["status"] == "open"
    assert conn.db["cases"][7]["manager_status"] is None
    assert conn.db["cases"][7]["closed_by"] is None
    assert conn.db["audit"][-1]["action"] == "hrbp_void"
    assert conn.db["audit"][-1]["actor"] == "hrbp:hazem"


def test_reopen_skips_case_not_closed_by_tl():
    conn = FakeConn({7: _case(7, closed_by="hrbp")})
    res = data.reopen_tl_cases(conn, [7], "hrbp:hazem", "reason")
    assert res["reopened"] == 0
    assert res["skipped"] == 1
    assert conn.db["cases"][7]["status"] == "closed"          # untouched
    assert conn.db["audit"] == []                              # no audit for a skip


def test_reopen_collects_and_clears_attachments():
    conn = FakeConn({7: _case(7)}, att={7: ["7/a.pdf", "7/b.jpg"]})
    res = data.reopen_tl_cases(conn, [7], "hrbp:hazem", "redo")
    assert res["attachment_paths"] == ["7/a.pdf", "7/b.jpg"]   # returned for the caller to purge
    assert conn.db["att"][7] == []                             # rows removed


def test_reopen_empty_list_is_noop():
    conn = FakeConn({7: _case(7)})
    res = data.reopen_tl_cases(conn, [], "hrbp:hazem", "reason")
    assert res == {"reopened": 0, "skipped": 0, "attachment_paths": []}
    assert conn.db["cases"][7]["status"] == "closed"


def test_reopen_mixed_batch_reports_counts():
    conn = FakeConn({1: _case(1), 2: _case(2, closed_by="hrbp"), 3: _case(3)})
    res = data.reopen_tl_cases(conn, [1, 2, 3], "hrbp:hazem", "reason")
    assert res["reopened"] == 2
    assert res["skipped"] == 1


def test_list_tl_submitted_returns_only_tl_finalized():
    conn = FakeConn({
        1: _case(1, manager_id=5, closed_by="tl"),
        2: _case(2, manager_id=5, closed_by="hrbp"),
        3: _case(3, manager_id=5, status="open", closed_by=None),
        4: _case(4, manager_id=9, closed_by="tl"),
    })
    rows = data.list_tl_submitted_cases(conn, 5)
    assert {r["id"] for r in rows} == {1}
