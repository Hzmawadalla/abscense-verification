"""Waive (2026-11-12) against real PostgreSQL: only cases no TL has answered are closed as Absent;
the TL can no longer answer them; the export and the effective status read Absent."""
import datetime
import importlib.util
import json
from pathlib import Path

from app import data, waive

_spec = importlib.util.spec_from_file_location(
    "p2_effective", Path(__file__).with_name("test_phase2_effective.py"))
p2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(p2)
org = p2.org                                    # TL-A (E-1, E-2), TL-B (E-3)
_q, _load, _cell, _flag, _case, _audit = p2._q, p2._load, p2._cell, p2._flag, p2._case, p2._audit
DAY, LATER = p2.DAY, p2.DAY + datetime.timedelta(days=1)


def _three_open_cases(conn):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger"), _cell("E-2", DAY, "Absent", "trigger"),
                 _cell("E-3", LATER, "Absent", "trigger", tl="TL-B")],
          [_flag("E-1", DAY), _flag("E-2", DAY), _flag("E-3", LATER, tl="TL-B")])
    return {crm: _case(conn, crm, LATER if crm == "E-3" else DAY)["id"]
            for crm in ("E-1", "E-2", "E-3")}


def test_list_waivable_holds_only_unanswered_open_cases(conn, org):
    ids = _three_open_cases(conn)
    p2._answer(conn, ids["E-2"], "present")                     # TL answered: not waivable
    rows = data.list_waivable_cases(conn)
    assert sorted(r["employee_crm"] for r in rows) == ["E-1", "E-3"]
    assert {r["manager_name"] for r in rows} == {"TL-A", "TL-B"}
    assert all(r["manager_id"] for r in rows)


def test_a_case_the_newest_upload_no_longer_flags_is_marked_review(conn, org):
    _three_open_cases(conn)
    _load(conn, [_cell("E-1", DAY, "Normal", "skip")], name="newer")     # E-1's day cleared
    flags = {r["employee_crm"]: r["unflagged"] for r in data.list_waivable_cases(conn)}
    assert flags == {"E-1": True, "E-2": False, "E-3": False}


def test_waive_closes_as_absent_audits_and_leaves_the_tl_answer_alone(conn, org):
    ids = _three_open_cases(conn)
    p2._answer(conn, ids["E-2"], "present")
    res = data.waive_cases(conn, list(ids.values()), "hrbp:hr@test", "TL on leave")
    assert res == {"waived": 2, "skipped": 1}

    e1 = _case(conn, "E-1")
    assert (e1["status"], e1["final_status"], e1["closed_by"]) == ("closed", "absent",
                                                                   "hrbp_waived")
    assert e1["closed_at"] is not None and e1["manager_status"] is None
    e2 = _case(conn, "E-2")
    assert (e2["manager_status"], e2["closed_by"]) == ("present", "tl")          # untouched

    log = _audit(conn, ids["E-1"], waive.WAIVE_ACTION)
    assert len(log) == 1 and log[0]["actor"] == "hrbp:hr@test"
    new = log[0]["new_value"] if isinstance(log[0]["new_value"], dict) \
        else json.loads(log[0]["new_value"])
    assert new["reason"] == "TL on leave" and new["final_status"] == "absent"
    assert _audit(conn, ids["E-2"], waive.WAIVE_ACTION) == []


def test_a_waived_case_leaves_the_tl_page_and_cannot_be_answered(conn, org):
    ids = _three_open_cases(conn)
    data.waive_cases(conn, [ids["E-1"]], "hrbp:x", "r")
    tl_a = _q(conn, "select * from attendance.managers where crm = 'TL-A'")[0]
    assert [r["employee_crm"] for r in data.tl_cases_for_manager(conn, tl_a)] == ["E-2"]
    assert not data.submit_verdict(conn, ids["E-1"], "present", None, "late", "tl:TL-A")
    assert _case(conn, "E-1")["final_status"] == "absent"


def test_a_waived_case_reads_absent_everywhere(conn, org):
    ids = _three_open_cases(conn)
    data.waive_cases(conn, [ids["E-1"]], "hrbp:x", "r")
    row = next(r for r in data.list_cases(conn) if r["id"] == ids["E-1"])
    assert (row["effective_status"], row["effective_source"]) == ("Absent", "hrbp_waived")
    closed = {(r["employee_crm"], r["final_status"]) for r in data.list_closed_cases(conn)}
    assert ("E-1", "absent") in closed
    assert "E-1" not in {r["employee_crm"] for r in data.list_unresolved_cases(conn)}
    assert {m["crm"]: m["open_cases"] for m in data.managers_overview(conn)}["TL-A"] == 1


def test_waiving_twice_is_harmless(conn, org):
    ids = _three_open_cases(conn)
    assert data.waive_cases(conn, [ids["E-1"]], "hrbp:x", "r") == {"waived": 1, "skipped": 0}
    assert data.waive_cases(conn, [ids["E-1"]], "hrbp:y", "r") == {"waived": 0, "skipped": 1}
    assert len(_audit(conn, ids["E-1"], waive.WAIVE_ACTION)) == 1


def test_an_hrbp_override_still_works_after_a_waive(conn, org):
    ids = _three_open_cases(conn)
    data.waive_cases(conn, [ids["E-1"]], "hrbp:x", "r")
    assert data.close_case(conn, ids["E-1"], "hrbp:x", "present", None, "proof arrived")
    row = next(r for r in data.list_cases(conn) if r["id"] == ids["E-1"])
    assert row["effective_source"] == "hrbp_override"
