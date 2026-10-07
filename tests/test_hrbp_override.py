"""Contract for the HRBP Override (close_case with a final_status).

Phase 0: an override records its value, actor, time and reason in its own columns and never
modifies the TL's verdict, comment or answer time; the audit snapshot keeps those plus any
previous override, so repeated overrides stay traceable.
"""
import datetime
import json

from app import data

RESPONDED_AT = datetime.datetime(2026, 9, 2, 8, 0, tzinfo=datetime.timezone.utc)
COLUMNS = ("status", "manager_status", "leave_type", "manager_comment", "manager_responded_at",
           "final_status", "final_leave_type", "closed_by",
           "hrbp_override_note", "hrbp_override_by", "hrbp_override_at")


def _tl_case():
    return {"status": "closed", "manager_status": "absent", "leave_type": None,
            "manager_comment": "on leave per TL", "manager_responded_at": RESPONDED_AT,
            "final_status": "absent", "final_leave_type": None, "closed_by": "tl",
            "hrbp_override_note": None, "hrbp_override_by": None, "hrbp_override_at": None}


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._fetch = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.lower().split())
        p = params or ()
        self.db["sql"].append(s)
        if s.startswith("select status, manager_status, leave_type, manager_comment, "
                        "manager_responded_at, final_status"):
            row = self.db["cases"].get(p[0])
            self._fetch = {k: row[k] for k in COLUMNS} if row else None
        elif s.startswith("update attendance.cases set status = 'closed', final_status = %s, "
                          "final_leave_type = %s, closed_by = 'hrbp', closed_at = now(), "
                          "hrbp_override_note"):
            final, leave, note, actor, cid = p
            row = self.db["cases"][cid]
            row.update(final_status=final, final_leave_type=leave, closed_by="hrbp",
                       hrbp_override_note=note, hrbp_override_by=actor, hrbp_override_at="now")
            self.rowcount = 1
        elif s.startswith("update attendance.cases set status = 'closed'"):
            final, leave, cid = p
            self.db["cases"][cid].update(final_status=final, final_leave_type=leave,
                                         closed_by="hrbp")
            self.rowcount = 1
        elif s.startswith("insert into attendance.audit_log"):
            self.db["audit"].append({"case_id": p[0], "actor": p[1], "action": p[2],
                                     "old": json.loads(p[3]), "new": json.loads(p[4])})

    def fetchone(self):
        return self._fetch


class FakeConn:
    def __init__(self, cases):
        self.db = {"cases": cases, "audit": [], "sql": []}

    def cursor(self, row_factory=None):
        return FakeCursor(self.db)

    def commit(self):
        pass

    def rollback(self):
        pass


def _override(conn, verdict="annual_leave", note="approved leave, see HR ticket"):
    return data.close_case(conn, 1, "hrbp:hr@51talk.com", final_status=verdict, comment=note)


def test_override_never_modifies_the_tl_answer():
    conn = FakeConn({1: _tl_case()})
    assert _override(conn)
    row = conn.db["cases"][1]
    assert row["manager_status"] == "absent"
    assert row["manager_comment"] == "on leave per TL"      # previously overwritten by the reason
    assert row["manager_responded_at"] == RESPONDED_AT
    assert not any("manager_comment" in q for q in conn.db["sql"] if q.startswith("update"))


def test_override_records_value_actor_time_and_note():
    conn = FakeConn({1: _tl_case()})
    _override(conn)
    row = conn.db["cases"][1]
    assert row["final_status"] == "annual_leave"
    assert row["hrbp_override_by"] == "hrbp:hr@51talk.com"
    assert row["hrbp_override_at"] == "now"
    assert row["hrbp_override_note"] == "approved leave, see HR ticket"
    audit = conn.db["audit"][-1]
    assert audit["action"] == "hrbp_override" and audit["actor"] == "hrbp:hr@51talk.com"
    assert audit["new"]["hrbp_override_note"] == "approved leave, see HR ticket"


def test_override_audit_snapshot_keeps_tl_answer_and_previous_override():
    conn = FakeConn({1: _tl_case()})
    _override(conn, "sick_leave", "first")
    _override(conn, "annual_leave", "second")
    old = conn.db["audit"][-1]["old"]
    assert old["manager_status"] == "absent"
    assert old["manager_comment"] == "on leave per TL"
    assert old["manager_responded_at"] == str(RESPONDED_AT)
    assert old["final_status"] == "sick_leave"               # the override being replaced
    assert old["hrbp_override_note"] == "first"


def test_accept_path_still_finalizes_tl_verdict_without_override_fields():
    conn = FakeConn({1: _tl_case()})
    assert data.close_case(conn, 1, "hrbp:hr@51talk.com")
    row = conn.db["cases"][1]
    assert row["final_status"] == "absent"
    assert row["hrbp_override_by"] is None
    assert row["manager_comment"] == "on leave per TL"
    assert conn.db["audit"][-1]["action"] == "hrbp_close"


def test_override_on_missing_case_is_rejected():
    conn = FakeConn({})
    assert data.close_case(conn, 1, "hrbp:x", final_status="present", comment="r") is False
    assert conn.db["audit"] == []
