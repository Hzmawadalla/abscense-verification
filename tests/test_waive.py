"""Waive (2026-11-12): HRBP closes cases no TL has answered yet as Absent, all or a chosen set.

Pure rules (app.waive) plus data.waive_cases against an in-memory fake, in the spirit of
test_void_tl. The real-database behaviour is in tests/pg/test_waive_pg.py.
"""
import datetime as dt
import json
import uuid

import pytest

from app import data, effective, waive

D = dt.date


# --------------------------------------------------------------------------- pure rules
def test_reason_is_required_and_trimmed():
    assert waive.validate_reason("  TL on leave ") == "TL on leave"
    for bad in (None, "", "   ", "​ "):
        with pytest.raises(ValueError):
            waive.validate_reason(bad)


def test_reason_has_an_upper_bound():
    with pytest.raises(ValueError):
        waive.validate_reason("x" * (waive.MAX_REASON + 1))


def u(n):
    """A stable case id: cases are keyed by UUID."""
    return uuid.UUID(int=n)


def test_ids_are_uuids_deduplicated_in_order():
    assert waive.normalize_ids([u(3), u(1), str(u(3)), u(2)]) == (u(3), u(1), u(2))
    for bad in (["1"], [1], [True], [None], ["not-a-uuid"]):
        with pytest.raises(ValueError):
            waive.normalize_ids(bad)


def _row(cid, crm="E-1", manager_id=1, day=D(2026, 11, 3), name=None):
    return {"id": u(cid), "employee_crm": crm, "employee_name": name or crm, "manager_id": manager_id,
            "manager_name": f"TL-{manager_id}", "work_date": day}


ROWS = [_row(1, "E-1", 1, D(2026, 11, 1)), _row(2, "E-1", 1, D(2026, 11, 5)),
        _row(3, "E-2", 2, D(2026, 11, 3)), _row(4, "e-3", 2, D(2026, 11, 9))]


def test_no_filters_keep_every_case():
    assert [r["id"].int for r in waive.filter_cases(ROWS)] == [1, 2, 3, 4]


def test_filters_combine():
    assert [r["id"].int for r in waive.filter_cases(ROWS, employee_crm="E-1")] == [1, 2]
    assert [r["id"].int for r in waive.filter_cases(ROWS, manager_id=2)] == [3, 4]
    assert [r["id"].int for r in waive.filter_cases(ROWS, date_from=D(2026, 11, 3),
                                                date_to=D(2026, 11, 5))] == [2, 3]
    assert [r["id"].int for r in waive.filter_cases(ROWS, manager_id=1,
                                                date_from=D(2026, 11, 2))] == [2]


def test_employee_filter_matches_crm_case_insensitively_never_by_name():
    rows = [_row(1, "E-3", name="Same Name"), _row(2, "E-4", name="Same Name")]
    assert [r["id"].int for r in waive.filter_cases(rows, employee_crm=" e-3 ")] == [1]


def test_filter_returns_new_list_and_leaves_input_alone():
    before = [dict(r) for r in ROWS]
    out = waive.filter_cases(ROWS, manager_id=1)
    assert out is not ROWS and ROWS == before


def test_no_team_leader_option_is_its_own_filter_not_all():
    rows = [*ROWS, _row(5, "E-9", None)]
    assert [r["id"].int for r in waive.filter_cases(rows, manager_id=waive.NO_TL)] == [5]
    assert len(waive.filter_cases(rows, manager_id=None)) == 5


def test_summary_counts_cases_employees_and_review_required():
    assert waive.summary(ROWS) == {"cases": 4, "employees": 3, "review": 0}
    assert waive.summary([]) == {"cases": 0, "employees": 0, "review": 0}
    assert waive.summary([{**ROWS[0], "unflagged": True}, ROWS[1]])["review"] == 1


def test_stage_take_and_cancel():
    state = {}
    staged = waive.stage(state, ROWS[:3], " TL on leave ")
    assert staged == {"ids": (u(1), u(2), u(3)), "reason": "TL on leave", "cases": 3, "employees": 2,
                      "review": 0}
    assert waive.pending(state) == staged
    assert waive.take(state) == staged and waive.pending(state) is None
    waive.stage(state, ROWS, "x")
    waive.cancel(state)
    assert waive.pending(state) is None and waive.take(state) is None


def test_stage_refuses_empty_selection_or_missing_reason():
    with pytest.raises(ValueError):
        waive.stage({}, [], "reason")
    with pytest.raises(ValueError):
        waive.stage({}, ROWS, "  ")


def test_a_waived_case_reads_absent_decided_by_waive():
    eff = effective.effective_status(case_status="closed", final_status=waive.WAIVE_VERDICT,
                                     closed_by=waive.WAIVE_CLOSED_BY, active_bucket="trigger")
    assert eff.status == "Absent" and eff.source == waive.WAIVE_CLOSED_BY
    assert not effective.is_undecided("closed", waive.WAIVE_CLOSED_BY, None, None)


# --------------------------------------------------------------------------- data.waive_cases
def _case(status="open", manager_status=None, override_at=None):
    return {"status": status, "manager_status": manager_status, "final_status": None,
            "closed_by": None, "hrbp_override_at": override_at}


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
        if s.startswith("select status, manager_status, final_status, closed_by, hrbp_override_at"):
            row = self.db["cases"].get(p[0].int)           # the fake keys cases by u(n).int
            self._fetch = dict(row) if row else None
        elif s.startswith("update attendance.cases set status = 'closed'"):
            final, closed_by, cid = p
            row = self.db["cases"].get(cid.int)
            ok = (row and row["status"] == "open" and row["manager_status"] is None
                  and row["hrbp_override_at"] is None)
            if ok:
                row.update(status="closed", final_status=final, closed_by=closed_by)
            self.rowcount = 1 if ok else 0
        elif s.startswith("insert into attendance.audit_log"):
            self.db["audit"].append({"case_id": p[0].int, "actor": p[1], "action": p[2],
                                     "old": json.loads(p[3]), "new": json.loads(p[4])})
        else:
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._fetch


class FakeConn:
    def __init__(self, cases):
        self.db = {"cases": cases, "audit": [], "sql": []}
        self.committed = 0

    def cursor(self, row_factory=None):
        return FakeCursor(self.db)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back = True


def test_a_failure_midway_rolls_everything_back(monkeypatch):
    conn = FakeConn({1: _case(), 2: _case()})
    real = FakeCursor.execute
    calls = {"audit": 0}

    def fail_second_audit(self, sql, params=None):
        if sql.lower().lstrip().startswith("insert into attendance.audit_log"):
            calls["audit"] += 1
            if calls["audit"] == 2:
                raise RuntimeError("db went away")
        return real(self, sql, params)
    monkeypatch.setattr(FakeCursor, "execute", fail_second_audit)
    with pytest.raises(RuntimeError):
        data.waive_cases(conn, [u(1), u(2)], "hrbp:a", "r")
    assert getattr(conn, "rolled_back", False) and conn.committed == 0


def test_waive_closes_unanswered_cases_as_absent_and_audits():
    conn = FakeConn({1: _case(), 2: _case()})
    assert data.waive_cases(conn, [u(1), u(2)], "hrbp:a@x", "TL on leave") == {"waived": 2, "skipped": 0}
    for cid in (1, 2):
        c = conn.db["cases"][cid]
        assert (c["status"], c["final_status"], c["closed_by"]) == ("closed", "absent",
                                                                     "hrbp_waived")
    a = conn.db["audit"][0]
    assert (a["case_id"], a["actor"], a["action"]) == (1, "hrbp:a@x", "hrbp_waive")
    assert a["new"] == {"status": "closed", "final_status": "absent",
                        "closed_by": "hrbp_waived", "reason": "TL on leave"}
    assert a["old"]["status"] == "open"
    assert conn.committed == 1


def test_waive_skips_answered_overridden_closed_and_missing_cases():
    conn = FakeConn({1: _case(manager_status="present"), 2: _case(override_at="t"),
                     3: _case(status="closed"), 4: _case()})
    assert data.waive_cases(conn, [u(1), u(2), u(3), u(4), u(99)],"hrbp:a", "r") == {"waived": 1, "skipped": 4}
    assert conn.db["cases"][1]["status"] == "open"            # the TL's answer is untouched
    assert conn.db["cases"][1]["manager_status"] == "present"
    assert [a["case_id"] for a in conn.db["audit"]] == [4]     # no audit entry for a skip


def test_waive_skips_a_case_answered_between_read_and_write(monkeypatch):
    conn = FakeConn({1: _case()})
    real = FakeCursor.execute

    def answer_first(self, sql, params=None):
        if sql.lower().lstrip().startswith("update attendance.cases"):
            self.db["cases"][1]["manager_status"] = "present"   # the TL answers right now
        return real(self, sql, params)
    monkeypatch.setattr(FakeCursor, "execute", answer_first)
    assert data.waive_cases(conn, [u(1)],"hrbp:a", "r") == {"waived": 0, "skipped": 1}
    assert conn.db["audit"] == []


def test_waive_validates_before_touching_the_database():
    conn = FakeConn({1: _case()})
    with pytest.raises(ValueError):
        data.waive_cases(conn, [u(1)],"hrbp:a", "   ")
    with pytest.raises(ValueError):
        data.waive_cases(conn, ["1"], "hrbp:a", "r")
    assert conn.db["sql"] == [] and conn.committed == 0


def test_waive_with_nothing_selected_does_nothing():
    conn = FakeConn({})
    assert data.waive_cases(conn, [], "hrbp:a", "r") == {"waived": 0, "skipped": 0}
    assert conn.db["sql"] == []


def test_update_guard_is_in_the_sql_itself():
    conn = FakeConn({1: _case()})
    data.waive_cases(conn, [u(1)],"hrbp:a", "r")
    upd = next(s for s in conn.db["sql"] if s.startswith("update"))
    for guard in ("status = 'open'", "manager_status is null", "hrbp_override_at is null"):
        assert guard in upd
