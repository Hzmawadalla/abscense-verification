"""Contract for voiding a TL's submissions: reopen closed-by-tl cases, keep evidence, audit.

Uses a purpose-built in-memory fake (same spirit as test_submit_verdict) so the reopen/skip/audit
logic is exercised without a database.
"""
import datetime
import json

from app import data

RESPONDED_AT = datetime.datetime(2026, 7, 6, 9, 30, tzinfo=datetime.timezone.utc)


def _case(cid, *, manager_id=1, status="closed", closed_by="tl",
          manager_status="present", employee_name="Emp", employee_crm="CRM-1"):
    return {"id": cid, "manager_id": manager_id, "status": status, "closed_by": closed_by,
            "manager_status": manager_status, "final_status": manager_status,
            "leave_type": None, "manager_comment": "note", "work_date": "2026-07-06",
            "manager_responded_at": RESPONDED_AT,
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
        self.db["sql"].append(s)
        if s.startswith("select status, manager_status, final_status, leave_type, "
                        "manager_comment, manager_responded_at, closed_by"):
            row = self.db["cases"].get(p[0])
            self._fetch = ({k: row[k] for k in ("status", "manager_status", "final_status",
                                                "leave_type", "manager_comment",
                                                "manager_responded_at", "closed_by")}
                           if row else None)
        elif s.startswith("update attendance.case_attachments set voided_at = now(), voided_by"):
            actor, cid = p
            hit = [a for a in self.db["att"].get(cid, []) if a["voided_at"] is None]
            for a in hit:
                a.update(voided_at="now", voided_by=actor)
            self._fetchall = [{"id": a["id"]} for a in hit]
        elif s.startswith("update attendance.cases set status = 'open'"):
            row = self.db["cases"].get(p[0])
            if row and row["closed_by"] == "tl" and row["status"] == "closed":
                row.update(status="open", manager_status=None, final_status=None,
                           leave_type=None, manager_comment=None, manager_responded_at=None,
                           closed_by=None)
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
        self.db = {"cases": cases or {}, "att": att or {}, "audit": [], "sql": []}
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


def _att(aid, path):
    return {"id": aid, "storage_path": path, "voided_at": None, "voided_by": None}


def test_reopen_keeps_attachment_rows_and_marks_them_voided():
    conn = FakeConn({7: _case(7)}, att={7: [_att("a1", "7/a.pdf"), _att("a2", "7/b.jpg")]})
    res = data.reopen_tl_cases(conn, [7], "hrbp:hazem", "redo")
    assert res["attachments_voided"] == 2
    assert [a["storage_path"] for a in conn.db["att"][7]] == ["7/a.pdf", "7/b.jpg"]  # rows kept
    assert all(a["voided_by"] == "hrbp:hazem" and a["voided_at"] for a in conn.db["att"][7])
    assert not any(q.startswith("delete") for q in conn.db["sql"])   # nothing deleted, anywhere
    assert json.loads(conn.db["audit"][-1]["new"])["voided_attachments"] == ["a1", "a2"]


def test_reopen_returns_no_storage_paths_to_purge():
    # The old contract handed storage paths back for the caller to delete; that hand-off is gone.
    conn = FakeConn({7: _case(7)}, att={7: [_att("a1", "7/a.pdf")]})
    res = data.reopen_tl_cases(conn, [7], "hrbp:hazem", "redo")
    assert "attachment_paths" not in res


def test_reopen_audit_snapshot_keeps_the_voided_answer():
    conn = FakeConn({7: _case(7)})
    data.reopen_tl_cases(conn, [7], "hrbp:hazem", "wrong verdict")
    old = json.loads(conn.db["audit"][-1]["old"])
    assert old["manager_status"] == "present"
    assert old["manager_comment"] == "note"
    assert old["manager_responded_at"] == str(RESPONDED_AT)   # timestamp survives (default=str)
    assert json.loads(conn.db["audit"][-1]["new"])["reason"] == "wrong verdict"


def test_reopen_does_not_touch_already_voided_attachments():
    earlier = {**_att("a0", "7/old.pdf"), "voided_at": "earlier", "voided_by": "hrbp:x"}
    conn = FakeConn({7: _case(7)}, att={7: [earlier, _att("a1", "7/new.pdf")]})
    res = data.reopen_tl_cases(conn, [7], "hrbp:hazem", "again")
    assert res["attachments_voided"] == 1
    assert conn.db["att"][7][0]["voided_by"] == "hrbp:x"      # first void's record unchanged


def test_reopen_empty_list_is_noop():
    conn = FakeConn({7: _case(7)})
    res = data.reopen_tl_cases(conn, [], "hrbp:hazem", "reason")
    assert res == {"reopened": 0, "skipped": 0, "attachments_voided": 0}
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
