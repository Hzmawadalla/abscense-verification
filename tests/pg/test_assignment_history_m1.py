"""Phase 3 M1 against real PostgreSQL: stale (older) reference files uploaded later, the advisory
warning read, concurrent uploads, and that team moves never touch answers, evidence or review
history. Synthetic data only."""
import datetime as dt
import threading
import time

import psycopg
from psycopg.rows import dict_row

from app import assignment_check
from app.attendance_view import tl_period_attendance
from ingestion import loader
from ingestion.db_psycopg import PsycopgDB
from ingestion.reference import Employee, Manager, ReferenceData

D = dt.date
P = D(2026, 9, 15)                                   # period 15 Sep -> 14 Oct


def _q(conn, sql, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall() if cur.description else None
    conn.commit()
    return rows


def _ref(assign):
    tls = sorted({t for t in assign.values() if t})
    return ReferenceData(managers=[Manager(t, t) for t in tls],
                         employees=[Employee(e, name=e, manager_crm=t) for e, t in assign.items()])


def _upload(conn, assign, on):
    with conn.transaction():
        loader.load_reference(PsycopgDB(conn), _ref(assign), seen_on=on)


def _stretches(conn, crm):
    return [(r["tl"], r["first_seen_on"], r["last_seen_on"]) for r in _q(
        conn, "select m.crm as tl, t.first_seen_on, t.last_seen_on from attendance.team_assignments t "
              "join attendance.employees e on e.id = t.employee_id "
              "left join attendance.managers m on m.id = t.manager_id "
              "where e.crm = %s order by t.first_seen_on, t.id", (crm,))]


def _tl(conn, crm):
    return _q(conn, "select id from attendance.managers where crm = %s", (crm,))[0]["id"]


def _team_days(conn, tl_crm, crm, period=P):
    return {r["work_date"] for r in tl_period_attendance(conn, _tl(conn, tl_crm), period)
            if r["employee_crm"] == crm and r["visibility"] == "team"}


# ------------------------------------------------------------------ older file uploaded later
def test_an_older_file_uploaded_later_is_flagged_before_loading(conn):
    _upload(conn, {"E-1": "TL-A", "E-2": "TL-A"}, D(2026, 9, 1))
    _upload(conn, {"E-1": "TL-B", "E-2": "TL-A"}, D(2026, 9, 20))       # the real move
    stale = _ref({"E-1": "TL-A", "E-2": "TL-A"})                        # the OLD Structure file
    changes = assignment_check.read_changes(conn, stale)
    assert changes.reverse == ("E-1",) and changes.moves == ("E-1",)
    assert any("older" in ln for ln in assignment_check.warning_lines(changes, {}))
    assert _stretches(conn, "E-1") == [("TL-A", D(2026, 9, 1), D(2026, 9, 1)),
                                       ("TL-B", D(2026, 9, 20), D(2026, 9, 20))]   # read-only


def test_loading_an_older_file_anyway_records_a_move_back_with_the_upload_date(conn):
    # the documented limitation: the warning is advisory; if HRBP loads it, the upload date is the
    # boundary — the earlier TL gets days from that date, the current TL's days stop before it
    _upload(conn, {"E-1": "TL-A"}, D(2026, 9, 1))
    _upload(conn, {"E-1": "TL-B"}, D(2026, 9, 20))
    _upload(conn, {"E-1": "TL-A"}, D(2026, 10, 2))                      # stale file loaded
    assert _stretches(conn, "E-1")[-1] == ("TL-A", D(2026, 10, 2), D(2026, 10, 2))
    assert _team_days(conn, "TL-B", "E-1") == {D(2026, 9, 20)}
    assert min(d for d in _team_days(conn, "TL-A", "E-1") if d > D(2026, 9, 20)) == D(2026, 10, 2)


def test_the_warning_read_reports_new_employees_and_losing_a_tl_without_writing(conn):
    _upload(conn, {"E-1": "TL-A", "E-2": "TL-A"}, D(2026, 9, 1))
    before = _q(conn, "select count(*) as n from attendance.team_assignments")[0]["n"]
    changes = assignment_check.read_changes(conn, _ref({"E-1": None, "E-2": "TL-A", "E-NEW": "TL-A"}))
    assert changes.to_none == ("E-1",) and changes.listed_existing == 2 and "E-NEW" not in changes.moves
    assert _q(conn, "select count(*) as n from attendance.team_assignments")[0]["n"] == before


def test_reprocessing_the_same_reference_the_same_day_adds_nothing(conn):
    for _ in range(3):
        _upload(conn, {"E-1": "TL-A"}, D(2026, 9, 1))
    assert _stretches(conn, "E-1") == [("TL-A", D(2026, 9, 1), D(2026, 9, 1))]
    assert assignment_check.read_changes(conn, _ref({"E-1": "TL-A"})).moves == ()


# ------------------------------------------------------------------ concurrent uploads
def _wait_for_a_lock_wait(dsn, timeout=10):
    with psycopg.connect(dsn, autocommit=True) as watch:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            n = watch.execute("select count(*) from pg_stat_activity where wait_event_type = 'Lock' "
                              "and datname = current_database()").fetchone()[0]
            if n:
                return True
            time.sleep(0.05)
    return False


def _concurrent(pg_dsn, first, second, on):
    """Upload `first` in an open transaction, start `second` concurrently, then commit `first`."""
    errors, c1, c2 = [], psycopg.connect(pg_dsn), psycopg.connect(pg_dsn)
    try:
        loader.load_reference(PsycopgDB(c1), _ref(first), seen_on=on)   # transaction stays open

        def run_second():
            try:
                c2.execute("set lock_timeout = '20s'")             # never hang the test run
                c2.commit()
                with c2.transaction():
                    loader.load_reference(PsycopgDB(c2), _ref(second), seen_on=on)
            except Exception as e:                                       # noqa: BLE001
                errors.append(e)
        t = threading.Thread(target=run_second)
        t.start()
        blocked = _wait_for_a_lock_wait(pg_dsn)
        c1.commit()
        t.join(15)
        return blocked, errors, t.is_alive()
    finally:
        c1.close()
        c2.close()


def test_two_concurrent_uploads_of_the_same_move_serialise_and_record_one_stretch(pg_dsn, conn):
    _upload(conn, {"E-1": "TL-A"}, D(2026, 9, 1))
    blocked, errors, alive = _concurrent(pg_dsn, {"E-1": "TL-B"}, {"E-1": "TL-B"}, D(2026, 10, 1))
    assert blocked and not errors and not alive          # the second waited for the first's row locks
    assert _stretches(conn, "E-1") == [("TL-A", D(2026, 9, 1), D(2026, 9, 1)),
                                       ("TL-B", D(2026, 10, 1), D(2026, 10, 1))]


def test_two_concurrent_uploads_with_different_tls_both_record_in_commit_order(pg_dsn, conn):
    _upload(conn, {"E-1": "TL-A"}, D(2026, 9, 1))
    _upload(conn, {"E-9": "TL-C"}, D(2026, 9, 1))                         # TL-C exists
    blocked, errors, alive = _concurrent(pg_dsn, {"E-1": "TL-B"}, {"E-1": "TL-C"}, D(2026, 10, 1))
    assert blocked and not errors and not alive
    assert [s[0] for s in _stretches(conn, "E-1")] == ["TL-A", "TL-B", "TL-C"]
    # the same-day rule: both TLs see that one day; the later commit is the current TL
    assert D(2026, 10, 1) in _team_days(conn, "TL-B", "E-1")
    assert D(2026, 10, 1) in _team_days(conn, "TL-C", "E-1")
    assert _team_days(conn, "TL-B", "E-1") == {D(2026, 10, 1)}


# ------------------------------------------------------------------ answers and evidence survive moves
def _snapshot(conn):
    return {t: _q(conn, f"select to_jsonb(x) as j from attendance.{t} x order by x.id")
            for t in ("cases", "audit_log", "case_attachments")}


def test_a_team_move_never_changes_answers_evidence_or_review_history(conn):
    _upload(conn, {"E-1": "TL-A"}, D(2026, 9, 1))
    _q(conn, "insert into attendance.cases (employee_id, manager_id, work_date, source_status, "
             "manager_status, manager_comment, manager_responded_at) select e.id, m.id, %s, "
             "'Absent', 'present', 'was on site', now() from attendance.employees e, "
             "attendance.managers m where e.crm = 'E-1' and m.crm = 'TL-A'", (D(2026, 9, 25),))
    _q(conn, "insert into attendance.audit_log (case_id, actor, action) "
             "select id, 'tl-token', 'tl_response' from attendance.cases")
    _q(conn, "insert into attendance.case_attachments (case_id, storage_path, filename) "
             "select id, 'evidence/x.png', 'x.png' from attendance.cases")
    before = _snapshot(conn)
    _upload(conn, {"E-1": "TL-B"}, D(2026, 10, 2))                      # the employee moves
    _upload(conn, {"E-1": "TL-A"}, D(2026, 10, 9))                      # and back
    assert _snapshot(conn) == before                                    # byte-for-byte unchanged
    rows = {r["work_date"]: r for r in tl_period_attendance(conn, _tl(conn, "TL-A"), P)
            if r["employee_crm"] == "E-1"}
    assert rows[D(2026, 9, 25)]["case_id"] and rows[D(2026, 9, 25)]["tl_answer"]   # A keeps it
    b_rows = [r for r in tl_period_attendance(conn, _tl(conn, "TL-B"), P) if r["employee_crm"] == "E-1"]
    assert b_rows and all(r["case_id"] is None and r["tl_answer"] is None for r in b_rows)  # B never sees it


# ------------------------------------------------------------------ approved first-TL rule (2026-10-18)
# No TL recorded at the start of tracking -> the first subsequently recorded TL owns all earlier
# history; a later transfer keeps the inclusive boundaries and the unowned gap.
P_OCT = D(2026, 10, 15)                             # period 15 Oct -> 14 Nov


def _days_in(period):
    end = D(period.year + (period.month == 12), period.month % 12 + 1, 14)
    return {period + dt.timedelta(i) for i in range((end - period).days + 1)}


def _sick_cell(conn, crm, day):
    _q(conn, "insert into attendance.ingestion_runs (source_filename, full_cells) values ('s', true) "
             "on conflict do nothing")
    _q(conn, "insert into attendance.attendance_days (ingestion_run_id, crm, crm_key, employee_id, "
             "work_date, raw_value, bucket, canonical_status, is_half_day) select "
             "(select min(id::text)::uuid from attendance.ingestion_runs), e.crm, lower(e.crm), e.id, "
             "%s, 'Sick Leave - surgery', 'skip', 'Sick Leave', false "
             "from attendance.employees e where e.crm = %s", (day, crm))


def test_first_tl_rule_tl_a_sees_all_earlier_days_of_an_employee_unmapped_at_tracking_start(conn):
    _upload(conn, {"E-1": None}, D(2026, 10, 20))                  # tracking start: no TL
    _upload(conn, {"E-1": "TL-A"}, D(2026, 11, 3))                 # first recorded TL
    assert _stretches(conn, "E-1") == [(None, D(2026, 10, 20), D(2026, 10, 20)),
                                       ("TL-A", D(2026, 11, 3), D(2026, 11, 3))]
    assert _team_days(conn, "TL-A", "E-1", P) == set(_days_in(P))          # a period before both
    assert _team_days(conn, "TL-A", "E-1", P_OCT) == set(_days_in(P_OCT))  # incl. the no-TL day


def test_first_tl_rule_then_a_transfer_keeps_boundaries_gap_and_the_view_rows(conn):
    _upload(conn, {"E-1": None, "E-9": "TL-C"}, D(2026, 10, 20))
    _upload(conn, {"E-1": "TL-A"}, D(2026, 10, 25))
    _upload(conn, {"E-1": "TL-A"}, D(2026, 11, 1))                 # A last seen 1 Nov
    _upload(conn, {"E-1": "TL-B"}, D(2026, 11, 8))                 # B first seen 8 Nov
    period = _days_in(P_OCT)
    a, b = _team_days(conn, "TL-A", "E-1", P_OCT), _team_days(conn, "TL-B", "E-1", P_OCT)
    assert a == {d for d in period if d <= D(2026, 11, 1)}
    assert b == {d for d in period if d >= D(2026, 11, 8)}
    gap = {d for d in period if D(2026, 11, 2) <= d <= D(2026, 11, 7)}
    assert gap and not gap & (a | b)
    rows = {r["work_date"]: r for r in tl_period_attendance(conn, _tl(conn, "TL-B"), P_OCT)
            if r["employee_crm"] == "E-1"}
    early = [r for d, r in rows.items() if d < D(2026, 11, 8)]
    assert early and all(r["visibility"] == "case_only" and r["status"] is None
                         and r["coverage"] == "none" for r in early)         # withheld for B
    assert "E-1" not in {r["employee_crm"] for r in tl_period_attendance(conn, _tl(conn, "TL-C"), P_OCT)}


def test_first_tl_rule_backfilled_days_carry_labels_only(conn):
    from app.attendance_view import TL_ROW_FIELDS
    _upload(conn, {"E-1": None}, D(2026, 10, 20))
    _sick_cell(conn, "E-1", D(2026, 9, 25))                         # before tracking started
    _upload(conn, {"E-1": "TL-A"}, D(2026, 11, 3))
    rows = [r for r in tl_period_attendance(conn, _tl(conn, "TL-A"), P) if r["employee_crm"] == "E-1"]
    day = next(r for r in rows if r["work_date"] == D(2026, 9, 25))
    assert day["visibility"] == "team" and day["status"] == "Leave"   # visible, as a label only
    assert all(tuple(r) == TL_ROW_FIELDS for r in rows)
    assert "surgery" not in repr(rows) and "Sick" not in repr(rows)


def test_first_tl_rule_employee_with_no_stretch_keeps_the_current_tl_rule(conn):
    _upload(conn, {"E-9": "TL-C"}, D(2026, 9, 1))                   # makes TL-C exist
    _q(conn, "insert into attendance.employees (crm, name, manager_id) select 'E-NS', 'no stretch', "
             "id from attendance.managers where crm = 'TL-C'")       # no stretch at all
    assert _stretches(conn, "E-NS") == []
    assert _team_days(conn, "TL-C", "E-NS", P) == set(_days_in(P))
