"""Phase 2 against real PostgreSQL: effective status, source changes that clear a flag, HRBP
override validation, D4 unmapped CRMs, the Pending export and the TL privacy boundary."""
import datetime
import json

import pytest
from psycopg.rows import dict_row

from app import data
from app.attendance_view import TL_ROW_FIELDS, hrbp_period_attendance, tl_period_attendance
from app.effective import BLANK, MISSING, PENDING, REVIEW_REQUIRED, UNRESOLVED_STATES
from ingestion import loader
from ingestion.db_psycopg import PsycopgDB
from ingestion.summary import (UNMAPPED_NO_FLAGS, UNMAPPED_NO_FLAGS_DETAIL, AttendanceCell,
                               CaseCandidate, IngestionException, IngestionResult)

D = datetime.date
P = D(2026, 9, 15)                       # the period 2026-09-15 -> 2026-10-14
DAY = D(2026, 9, 20)


def _q(conn, sql, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall() if cur.description else None
    conn.commit()
    return rows


@pytest.fixture()
def org(conn):
    """TL-A (E-1, E-2), TL-B (E-3)."""
    ids = {}
    for crm in ("TL-A", "TL-B"):
        ids[crm] = _q(conn, "insert into attendance.managers (crm, name) values (%s, %s) "
                            "returning id", (crm, crm))[0]["id"]
    for crm, tl in (("E-1", "TL-A"), ("E-2", "TL-A"), ("E-3", "TL-B")):
        ids[crm] = _q(conn, "insert into attendance.employees (crm, name, manager_id) "
                            "values (%s, %s, %s) returning id", (crm, crm, ids[tl]))[0]["id"]
    return ids


def _cell(crm, day, raw, bucket, tl="TL-A"):
    return AttendanceCell(crm=crm, crm_key=crm.lower(), work_date=day, raw_value=raw,
                          bucket=bucket, canonical_status=raw, is_half_day=False,
                          manager_crm=tl, source_row=4)


def _flag(crm, day, raw="Absent", tl="TL-A"):
    return CaseCandidate(employee_crm=crm, manager_crm=tl, work_date=day, source_status=raw)


def _load(conn, cells, cases=(), exceptions=(), name="wb.xlsx"):
    conn.commit()             # each upload its own transaction, as in the app (created_at order)
    res = IngestionResult(cases=list(cases), cells=list(cells), exceptions=list(exceptions),
                          stats={"date_range": (min(c.work_date for c in cells),
                                                max(c.work_date for c in cells))})
    with conn.transaction():
        run = loader.load_ingestion(PsycopgDB(conn), res, source_filename=name,
                                    triggered_by="hrbp:t").run_id
    conn.commit()
    return run


def _case(conn, crm, day=DAY):
    return _q(conn, "select c.* from attendance.cases c join attendance.employees e "
                    "on e.id = c.employee_id where e.crm = %s and c.work_date = %s",
              (crm, day))[0]


def _audit(conn, case_id, action):
    return _q(conn, "select * from attendance.audit_log where case_id = %s and action = %s "
                    "order by created_at", (case_id, action))


def _grid_row(conn, crm, day=DAY):
    return next(r for r in hrbp_period_attendance(conn, P)
                if r["employee_crm"] == crm and r["work_date"] == day)


def _answer(conn, case_id, verdict="absent", comment="seen it", actor="tl:TL-A"):
    assert data.submit_verdict(conn, case_id, verdict, None, comment, actor)


def _attach(conn, case_id):
    data.add_attachment(conn, case_id, f"cases/{case_id}/p.png", "p.png", "image/png", 10)


# ------------------------------------------------------------------ effective status on the grid
def test_grid_effective_status_for_each_kind_of_day(conn, org):
    d_pending, d_tl, d_override, d_leave, d_blank = (D(2026, 9, 16), D(2026, 9, 17),
                                                     D(2026, 9, 18), D(2026, 9, 19), DAY)
    _load(conn, [_cell("E-1", d_pending, "Absent", "trigger"),
                 _cell("E-1", d_tl, "Absent", "trigger"),
                 _cell("E-1", d_override, "No Show", "trigger"),
                 _cell("E-1", d_leave, "Annual Leave", "skip"),
                 _cell("E-1", d_blank, None, "blank")],
          [_flag("E-1", d_pending), _flag("E-1", d_tl), _flag("E-1", d_override, "No Show")])
    _answer(conn, _case(conn, "E-1", d_tl)["id"], "present")
    ov = _case(conn, "E-1", d_override)["id"]
    _answer(conn, ov, "absent")
    assert data.close_case(conn, ov, "hrbp:x", final_status="sick_leave", comment="certificate")

    got = {d: _grid_row(conn, "E-1", d) for d in (d_pending, d_tl, d_override, d_leave, d_blank)}
    assert (got[d_pending]["effective_status"], got[d_pending]["effective_source"]) == \
        (PENDING, "case")
    assert (got[d_tl]["effective_status"], got[d_tl]["effective_source"]) == ("Present", "tl")
    assert (got[d_override]["effective_status"], got[d_override]["effective_source"]) == \
        ("Sick Leave", "hrbp_override")
    assert got[d_override]["tl_answer"] == "absent"          # the TL layer is still there
    assert (got[d_leave]["effective_status"], got[d_leave]["effective_source"]) == \
        ("Annual Leave", "original")
    assert got[d_blank]["effective_status"] == BLANK
    # a day no upload covers is Missing — never Absent
    assert _grid_row(conn, "E-1", D(2026, 10, 1))["effective_status"] == MISSING


def test_tl_rows_never_carry_effective_status(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger")], [_flag("E-1", DAY)])
    rows = tl_period_attendance(conn, org["TL-A"], P)
    assert rows and all(tuple(r) == TL_ROW_FIELDS for r in rows)
    assert not any(k in r for r in rows for k in ("effective_status", "effective_source",
                                                  "needs_review", "raw_value"))


# ------------------------------------------------------------------ newer upload clears a flag
def _flag_then_clear(conn, clear_raw="Normal", clear_bucket="skip"):
    first = _load(conn, [_cell("E-1", DAY, "Absent", "trigger")], [_flag("E-1", DAY)], name="a")
    cid = _case(conn, "E-1")["id"]
    return first, cid, lambda: _load(conn, [_cell("E-1", DAY, clear_raw, clear_bucket)], name="b")


def test_unflag_keeps_the_open_case_and_marks_it_for_review(conn, org):
    _, cid, clear = _flag_then_clear(conn)
    before = _case(conn, "E-1")
    clear()
    after = _case(conn, "E-1")
    for col in ("status", "source_status", "manager_status", "final_status", "manager_id",
                "ingestion_run_id", "closed_by"):
        assert after[col] == before[col], col             # nothing voided, deleted or resolved
    entry = _audit(conn, cid, "source_unflagged")
    assert len(entry) == 1
    assert entry[0]["old_value"]["source_status"] == "Absent"
    assert entry[0]["new_value"]["raw_value"] == "Normal" and entry[0]["new_value"]["bucket"] == "skip"
    row = _grid_row(conn, "E-1")
    assert (row["effective_status"], row["needs_review"], row["pending"]) == \
        (REVIEW_REQUIRED, True, False)
    listed = next(r for r in data.list_cases(conn) if r["id"] == cid)
    assert (listed["effective_status"], listed["needs_review"]) == (REVIEW_REQUIRED, True)
    assert listed["active_raw"] == "Normal" and listed["source_status"] == "Absent"


def test_unflag_to_blank_is_also_review_required(conn, org):
    _, cid, clear = _flag_then_clear(conn, None, "blank")
    clear()
    assert len(_audit(conn, cid, "source_unflagged")) == 1
    assert _grid_row(conn, "E-1")["effective_status"] == REVIEW_REQUIRED


def test_unflag_preserves_answer_comment_evidence_and_override(conn, org):
    _, cid, clear = _flag_then_clear(conn)
    _attach(conn, cid)
    _answer(conn, cid, "absent", "was off")
    assert data.close_case(conn, cid, "hrbp:x", final_status="annual_leave", comment="approved")
    before = _case(conn, "E-1")
    n_audit = len(_q(conn, "select id from attendance.audit_log where case_id = %s", (cid,)))
    clear()
    after = _case(conn, "E-1")
    for col in ("status", "manager_status", "manager_comment", "manager_responded_at",
                "final_status", "hrbp_override_note", "hrbp_override_by", "hrbp_override_at",
                "closed_by", "closed_at"):
        assert after[col] == before[col], col
    assert len(data.list_attachments(conn, cid)) == 1
    audit = _q(conn, "select action from attendance.audit_log where case_id = %s", (cid,))
    assert len(audit) == n_audit + 1                       # appended, nothing rewritten
    assert len(_audit(conn, cid, "source_unflagged")) == 1
    row = _grid_row(conn, "E-1")
    assert (row["effective_status"], row["effective_source"], row["needs_review"]) == \
        ("Annual Leave", "hrbp_override", True)


def test_tl_answer_survives_unflag_and_is_flagged_for_review(conn, org):
    _, cid, clear = _flag_then_clear(conn)
    _answer(conn, cid, "present")
    clear()
    row = _grid_row(conn, "E-1")
    assert (row["effective_status"], row["effective_source"], row["needs_review"]) == \
        ("Present", "tl", True)


def test_reuploading_the_same_unflagged_file_adds_no_second_entry(conn, org):
    _, cid, clear = _flag_then_clear(conn)
    clear()
    clear()
    assert len(_audit(conn, cid, "source_unflagged")) == 1


def test_unchanged_flagged_reupload_adds_no_unflag_entry(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger")], [_flag("E-1", DAY)], name="a")
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger")], [_flag("E-1", DAY)], name="a2")
    cid = _case(conn, "E-1")["id"]
    assert _audit(conn, cid, "source_unflagged") == []
    assert _audit(conn, cid, "source_status_changed") == []
    assert _grid_row(conn, "E-1")["effective_status"] == PENDING


def test_unflag_only_touches_days_the_new_upload_covers(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger"),
                 _cell("E-2", DAY, "Absent", "trigger")],
          [_flag("E-1", DAY), _flag("E-2", DAY)], name="a")
    _load(conn, [_cell("E-1", DAY, "Normal", "skip")], name="b")      # E-2 not in this file
    assert _audit(conn, _case(conn, "E-2")["id"], "source_unflagged") == []
    assert _grid_row(conn, "E-2")["effective_status"] == PENDING


def test_unflagged_case_stays_on_the_tl_page_with_tl_safe_fields_only(conn, org):
    _, cid, clear = _flag_then_clear(conn)
    clear()
    mgr = _q(conn, "select id, crm from attendance.managers where crm = 'TL-A'")[0]
    rows = data.tl_cases_for_manager(conn, mgr)
    assert [r["id"] for r in rows] == [cid]
    assert set(rows[0]) == set(data.TL_CASE_FIELDS)


def test_another_tl_never_sees_the_previous_tls_answer_after_a_source_change(conn, org):
    _, cid, clear = _flag_then_clear(conn)
    _answer(conn, cid, "absent", "private note", actor="tl:TL-A")
    data.reopen_tl_cases(conn, [cid], "hrbp:x", "reassign")
    _q(conn, "update attendance.cases set manager_id = %s where id = %s", (org["TL-B"], cid))
    clear()
    mgr_b = _q(conn, "select id, crm from attendance.managers where crm = 'TL-B'")[0]
    row = next(r for r in data.tl_cases_for_manager(conn, mgr_b) if r["id"] == cid)
    assert row["verdict_label"] is None and row["own_comment"] is None
    assert "private note" not in json.dumps(data.tl_cases_for_manager(conn, mgr_b), default=str)


# ------------------------------------------------------------------ HRBP override validation
@pytest.mark.parametrize("bad", ["half_day", "excused", "Present"])
def test_unsupported_override_is_rejected_and_writes_nothing(conn, org, bad):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger")], [_flag("E-1", DAY)])
    case = _case(conn, "E-1")
    with pytest.raises(ValueError):
        data.close_case(conn, case["id"], "hrbp:x", final_status=bad, comment="why")
    conn.rollback()
    assert _case(conn, "E-1") == case
    assert _q(conn, "select id from attendance.audit_log where case_id = %s", (case["id"],)) == []


def test_override_decides_a_pending_case_and_keeps_the_tl_layer_empty(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger")], [_flag("E-1", DAY)])
    cid = _case(conn, "E-1")["id"]
    assert data.close_case(conn, cid, "hrbp:x", final_status="present", comment="CRM calls seen")
    after = _case(conn, "E-1")
    assert after["manager_status"] is None and after["hrbp_override_by"] == "hrbp:x"
    row = _grid_row(conn, "E-1")
    assert (row["effective_status"], row["effective_source"]) == ("Present", "hrbp_override")
    entry = _audit(conn, cid, "hrbp_override")
    assert len(entry) == 1 and entry[0]["new_value"]["final_status"] == "present"


def test_repeated_overrides_keep_the_previous_one_in_the_audit(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger")], [_flag("E-1", DAY)])
    cid = _case(conn, "E-1")["id"]
    data.close_case(conn, cid, "hrbp:x", final_status="present", comment="first")
    data.close_case(conn, cid, "hrbp:y", final_status="unpaid_leave", comment="second")
    entries = _audit(conn, cid, "hrbp_override")
    assert [e["new_value"]["final_status"] for e in entries] == ["present", "unpaid_leave"]
    assert entries[1]["old_value"]["final_status"] == "present"
    assert _grid_row(conn, "E-1")["effective_status"] == "Unpaid Leave"


# ------------------------------------------------------------------ D4 unmapped CRMs
def test_unmapped_crm_without_flags_is_listed_and_never_an_absence(conn, org):
    ghost = AttendanceCell(crm="E-GHOST", crm_key="e-ghost", work_date=DAY, raw_value="Normal",
                           bucket="skip", canonical_status="Normal", is_half_day=False,
                           manager_crm=None, source_row=9)
    exc = IngestionException("E-GHOST", None, UNMAPPED_NO_FLAGS_DETAIL, UNMAPPED_NO_FLAGS)
    _load(conn, [_cell("E-1", DAY, "Normal", "skip"), ghost], exceptions=[exc])
    listed = data.list_unmapped_no_flags(conn)
    assert [r["crm"] for r in listed] == ["E-GHOST"]
    assert listed[0]["detail"] == UNMAPPED_NO_FLAGS_DETAIL
    assert _q(conn, "select count(*) as n from attendance.cases")[0]["n"] == 0
    stored = _q(conn, "select employee_id, bucket from attendance.attendance_days "
                      "where crm_key = 'e-ghost'")
    assert stored == [{"employee_id": None, "bucket": "skip"}]
    assert "E-GHOST" not in {r["crm"] for r in data.list_reference_gaps(conn)}  # export unchanged
    assert "E-GHOST" not in {r["employee_crm"] for r in hrbp_period_attendance(conn, P)}


# ------------------------------------------------------------------ export inputs
def test_unresolved_cases_feed_the_export_and_closed_ones_do_not(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger"), _cell("E-2", DAY, "Absent", "trigger")],
          [_flag("E-1", DAY), _flag("E-2", DAY)])
    _answer(conn, _case(conn, "E-2")["id"], "present")
    unresolved = data.list_unresolved_cases(conn)
    closed = data.list_closed_cases(conn)
    assert [r["employee_crm"] for r in unresolved] == ["E-1"]
    assert [r["employee_crm"] for r in closed] == ["E-2"]
    assert set(unresolved[0]) >= {"employee_crm", "work_date", "source_status"}


# ------------------------------------------------------------------ period close (2026-10-16)
# Changed on purpose: the period close used to set final_status = 'absent' (closed_by
# 'hrbp_cutoff') on every still-flagged open case. Decision 2026-10-16: unresolved stays Pending,
# so the close now changes no case field and only records the cut-off.
def _case_fields(row):
    keep = ("status", "manager_status", "final_status", "closed_by", "closed_at", "leave_type",
            "manager_comment", "manager_responded_at", "hrbp_override_at", "source_status")
    return {k: row[k] for k in keep}


def test_period_close_leaves_unresolved_cases_open_with_no_final_status(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger"), _cell("E-2", DAY, "Absent", "trigger")],
          [_flag("E-1", DAY), _flag("E-2", DAY)], name="a")
    _load(conn, [_cell("E-2", DAY, None, "blank")], name="b")         # E-2's day now blank
    before = {crm: _case_fields(_case(conn, crm)) for crm in ("E-1", "E-2")}
    res = data.close_open_month(conn, "hrbp:x")
    assert res == {"pending": 1, "kept_for_review": 1}
    for crm in ("E-1", "E-2"):
        assert _case_fields(_case(conn, crm)) == before[crm]            # nothing on the case moved
        assert _case(conn, crm)["status"] == "open" and _case(conn, crm)["final_status"] is None
    assert _grid_row(conn, "E-1")["effective_status"] == PENDING
    assert _grid_row(conn, "E-2")["effective_status"] == REVIEW_REQUIRED
    assert _audit(conn, _case(conn, "E-1")["id"], "auto_close_absent") == []
    e1_note = _audit(conn, _case(conn, "E-1")["id"], data.PERIOD_CLOSE_ACTION)
    e2_note = _audit(conn, _case(conn, "E-2")["id"], data.PERIOD_CLOSE_ACTION)
    assert [n["new_value"]["effective"] for n in e1_note + e2_note] == ["pending", "review_required"]
    assert e1_note[0]["new_value"]["final_status"] is None


def test_period_close_never_produces_absent_anywhere(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger")], [_flag("E-1", DAY)])
    data.close_open_month(conn, "hrbp:x")
    assert [r["employee_crm"] for r in data.list_unresolved_cases(conn)] == ["E-1"]
    assert data.list_closed_cases(conn) == []
    assert [r["effective_status"] for r in data.list_cases(conn)] == [PENDING]


def test_period_close_keeps_history_audit_and_evidence(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger")], [_flag("E-1", DAY)], name="a")
    cid = _case(conn, "E-1")["id"]
    _answer(conn, cid, "present")
    _attach(conn, cid)
    data.reopen_tl_cases(conn, [cid], "hrbp:x", "wrong screenshot")    # voided answer, kept
    audit_before = _q(conn, "select id, action from attendance.audit_log where case_id = %s "
                            "order by created_at", (cid,))
    data.close_open_month(conn, "hrbp:x")
    audit_after = _q(conn, "select id, action from attendance.audit_log where case_id = %s "
                           "order by created_at", (cid,))
    assert audit_after[:len(audit_before)] == audit_before             # append-only
    assert [a["action"] for a in audit_after[len(audit_before):]] == [data.PERIOD_CLOSE_ACTION]
    assert len(data.list_attachments(conn, cid, include_voided=True)) == 1


def test_period_close_leaves_decided_cases_alone(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger"), _cell("E-2", DAY, "Absent", "trigger")],
          [_flag("E-1", DAY), _flag("E-2", DAY)])
    _answer(conn, _case(conn, "E-1")["id"], "absent")                  # an explicit Absent
    data.close_case(conn, _case(conn, "E-2")["id"], "hrbp:x", final_status="annual_leave",
                    comment="approved")
    before = {crm: _case_fields(_case(conn, crm)) for crm in ("E-1", "E-2")}
    assert data.close_open_month(conn, "hrbp:x") == {"pending": 0, "kept_for_review": 0}
    assert {crm: _case_fields(_case(conn, crm)) for crm in ("E-1", "E-2")} == before
    assert _grid_row(conn, "E-1")["effective_status"] == "Absent"     # explicit Absent stays
    assert [(r["employee_crm"], r["final_status"]) for r in data.list_closed_cases(conn)] == \
        [("E-1", "absent"), ("E-2", "annual_leave")]


def test_tl_can_still_answer_after_the_period_close(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger")], [_flag("E-1", DAY)])
    data.close_open_month(conn, "hrbp:x")
    tl_a = _q(conn, "select * from attendance.managers where crm = 'TL-A'")[0]
    assert [r["employee_crm"] for r in data.tl_cases_for_manager(conn, tl_a)] == ["E-1"]
    _answer(conn, _case(conn, "E-1")["id"], "present")
    assert _grid_row(conn, "E-1")["effective_status"] == "Present"


def test_close_sends_nothing_and_reminder_counts_follow_the_existing_manual_model(conn, org):
    # There is no scheduler: reminders are HRBP's manual Email / DingTalk sends to "TLs with open
    # cases" (data.managers_overview). The close sends nothing itself, and a case left open by it
    # is still counted for its TL - no closed-period marker exists to tell it apart (PR notes).
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger")], [_flag("E-1", DAY)])
    data.close_open_month(conn, "hrbp:x")
    assert _q(conn, "select count(*) as n from attendance.notifications")[0]["n"] == 0
    by_tl = {m["crm"]: m["open_cases"] for m in data.managers_overview(conn)}
    assert by_tl == {"TL-A": 1, "TL-B": 0}


# ------------------------------------------------------------------ historical hrbp_cutoff rows
def _old_cutoff(conn, crm, manager_status=None):
    """A row as the pre-2026-10 period close left it: closed, final_status absent, hrbp_cutoff."""
    _q(conn, "update attendance.cases c set status = 'closed', final_status = 'absent', "
             "manager_status = %s, closed_by = 'hrbp_cutoff', closed_at = now() "
             "from attendance.employees e where e.id = c.employee_id and e.crm = %s",
       (manager_status, crm))
    return _case(conn, crm)


def test_old_cutoff_absent_reads_pending_everywhere_and_is_not_rewritten(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger")], [_flag("E-1", DAY)])
    stored = _case_fields(_old_cutoff(conn, "E-1"))
    row = data.list_cases(conn)[0]
    assert (row["final_status"], row["closed_by"]) == ("absent", "hrbp_cutoff")   # stored, shown
    assert (row["effective_status"], row["effective_source"]) == (PENDING, "case")
    assert _grid_row(conn, "E-1")["effective_status"] == PENDING
    unresolved = data.list_unresolved_cases(conn)
    assert [(r["employee_crm"], r["closed_by"]) for r in unresolved] == [("E-1", "hrbp_cutoff")]
    assert data.list_closed_cases(conn) == []                          # never exported as Absent
    data.close_open_month(conn, "hrbp:x")
    assert _case_fields(_case(conn, "E-1")) == stored                  # history kept as stored


def test_old_cutoff_on_a_no_longer_flagged_day_is_review_required(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger")], [_flag("E-1", DAY)], name="a")
    _old_cutoff(conn, "E-1")
    _load(conn, [_cell("E-1", DAY, "Normal", "skip")], name="b")
    row = data.list_cases(conn)[0]
    assert (row["effective_status"], row["needs_review"]) == (REVIEW_REQUIRED, True)


def test_old_cutoff_with_a_tl_answer_exports_the_tl_answer(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger")], [_flag("E-1", DAY)])
    _old_cutoff(conn, "E-1", manager_status="present")
    assert data.list_unresolved_cases(conn) == []
    assert [(r["employee_crm"], r["final_status"]) for r in data.list_closed_cases(conn)] == \
        [("E-1", "present")]
    assert data.list_cases(conn)[0]["effective_status"] == "Present"


def test_hrbp_override_decides_an_old_cutoff_case(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger")], [_flag("E-1", DAY)])
    cid = _old_cutoff(conn, "E-1")["id"]
    assert data.close_case(conn, cid, "hrbp:x", final_status="absent", comment="confirmed")
    assert data.list_unresolved_cases(conn) == []
    assert [(r["final_status"], r["closed_by"]) for r in data.list_closed_cases(conn)] == \
        [("absent", "hrbp")]
    row = data.list_cases(conn)[0]
    assert (row["effective_status"], row["effective_source"]) == ("Absent", "hrbp_override")


def test_sql_and_python_agree_on_what_is_undecided(conn, org):
    # data.CASE_UNDECIDED (export) and app.effective.is_undecided (screen) must never drift apart
    days = [DAY + datetime.timedelta(days=i) for i in range(5)]
    _load(conn, [_cell("E-1", d, "Absent", "trigger") for d in days],
          [_flag("E-1", d) for d in days])
    ids = [_case(conn, "E-1", d)["id"] for d in days]
    _answer(conn, ids[1], "absent")                                      # TL decided
    data.close_case(conn, ids[2], "hrbp:x", final_status="present", comment="ok")   # override
    _q(conn, "update attendance.cases set status = 'closed', final_status = 'absent', "
             "closed_by = 'hrbp_cutoff' where id = any(%s)", ([ids[3], ids[4]],))
    _q(conn, "update attendance.cases set manager_status = 'present' where id = %s", (ids[4],))
    sql = {r["work_date"] for r in data.list_unresolved_cases(conn)}
    py = {r["work_date"] for r in data.list_cases(conn)
          if r["effective_status"] in UNRESOLVED_STATES}
    assert sql == py == {days[0], days[3]}


def test_every_case_lands_in_exactly_one_export_list_matching_the_screen(conn, org):
    # review M1/M2: legacy shapes no writer produces today must still agree with the screen and
    # never fall out of both lists: a responded case with a TL answer, a closed row with NULL
    # closed_by, a closed-by-TL row
    days = [DAY + datetime.timedelta(days=i) for i in range(4)]
    _load(conn, [_cell("E-1", d, "Absent", "trigger") for d in days],
          [_flag("E-1", d) for d in days])
    ids = [_case(conn, "E-1", d)["id"] for d in days]
    _q(conn, "update attendance.cases set status = 'manager_responded', manager_status = 'present' "
             "where id = %s", (ids[0],))
    _q(conn, "update attendance.cases set status = 'closed', final_status = 'absent', "
             "closed_by = null where id = %s", (ids[1],))
    _answer(conn, ids[2], "absent")
    unresolved = {r["work_date"] for r in data.list_unresolved_cases(conn)}
    closed = {r["work_date"]: r["final_status"] for r in data.list_closed_cases(conn)}
    assert unresolved == {days[3]} and not (unresolved & set(closed))
    assert closed == {days[0]: "present", days[1]: "absent", days[2]: "absent"}
    screen = {r["work_date"]: r["effective_status"] for r in data.list_cases(conn)}
    assert screen == {days[0]: "Present", days[1]: "Absent", days[2]: "Absent", days[3]: PENDING}


def test_pressing_close_twice_writes_one_entry_per_case(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger")], [_flag("E-1", DAY)])
    first = data.close_open_month(conn, "hrbp:x")
    second = data.close_open_month(conn, "hrbp:y")
    assert first == second == {"pending": 1, "kept_for_review": 0}
    assert len(_audit(conn, _case(conn, "E-1")["id"], data.PERIOD_CLOSE_ACTION)) == 1


# ------------------------------------------------------------------ D4 goes away once mapped
def test_d4_entry_disappears_once_the_crm_is_an_employee(conn, org):
    ghost = AttendanceCell(crm="E-GHOST", crm_key="e-ghost", work_date=DAY, raw_value="Normal",
                           bucket="skip", canonical_status="Normal", is_half_day=False,
                           manager_crm=None, source_row=9)
    _load(conn, [ghost], exceptions=[IngestionException("E-GHOST", None, UNMAPPED_NO_FLAGS_DETAIL,
                                                        UNMAPPED_NO_FLAGS)])
    assert [r["crm"] for r in data.list_unmapped_no_flags(conn)] == ["E-GHOST"]
    _q(conn, "insert into attendance.employees (crm, name, manager_id) values ('e-ghost', 'G', %s)",
       (org["TL-A"],))
    assert data.list_unmapped_no_flags(conn) == []


# ------------------------------------------------------------------ uploads and un-flag history
def test_unflag_history_protects_the_original_upload_not_the_newer_one(conn, org):
    first, cid, clear = _flag_then_clear(conn)
    clear()
    newer = _q(conn, "select id from attendance.ingestion_runs where source_filename = 'b'")[0]["id"]
    assert data.remove_upload(conn, first) == {"refused": 1}           # history is never deleted
    assert data.remove_upload(conn, newer) == {"cases_deleted": 0}     # its own cells only
    assert len(_audit(conn, cid, "source_unflagged")) == 1             # the history remains
    assert _grid_row(conn, "E-1")["effective_status"] == PENDING       # flagged again
