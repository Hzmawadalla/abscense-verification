"""Phase 1 against real PostgreSQL: migration safety, SQL/Python period agreement, cell storage,
active-source resolution, remove/reset, and the TL/HRBP period data contract."""
import datetime
from pathlib import Path

import pytest
from psycopg.errors import CheckViolation, RaiseException, UniqueViolation
from psycopg.rows import dict_row

from app import data
from app.attendance_view import hrbp_period_attendance, tl_period_attendance
from ingestion import loader
from ingestion.db_psycopg import PsycopgDB
from ingestion.periods import attendance_period
from ingestion.summary import AttendanceCell, CaseCandidate, IngestionResult

D = datetime.date
P = D(2026, 9, 15)                                     # the period 2026-09-15 -> 2026-10-14
MIGRATION = next((Path(__file__).resolve().parents[2] / "supabase" / "migrations")
                 .glob("*_phase1_attendance_days.sql"))


def _q(conn, sql, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall() if cur.description else None
    conn.commit()
    return rows


@pytest.fixture()
def org(conn):
    """TL-A (E-1, E-2), TL-B (E-3); E-2 joined mid-period, E-3 left mid-period."""
    ids = {}
    for crm in ("TL-A", "TL-B"):
        ids[crm] = _q(conn, "insert into attendance.managers (crm, name) values (%s, %s) "
                            "returning id", (crm, crm))[0]["id"]
    for crm, name, tl, join, exit_ in [("E-1", "Emp One", "TL-A", None, None),
                                       ("E-2", "Emp Two", "TL-A", D(2026, 9, 20), None),
                                       ("E-3", "Emp Three", "TL-B", None, D(2026, 10, 1))]:
        ids[crm] = _q(conn, "insert into attendance.employees (crm, name, manager_id, join_date, "
                            "exit_date) values (%s, %s, %s, %s, %s) returning id",
                      (crm, name, ids[tl], join, exit_))[0]["id"]
    return ids


def _cell(crm, day, raw, bucket="skip", canon=None, hd=False, tl="TL-A", row=4):
    return AttendanceCell(crm=crm, crm_key=crm.lower(), work_date=day, raw_value=raw,
                          bucket=bucket, canonical_status=canon if canon is not None else raw,
                          is_half_day=hd, manager_crm=tl, source_row=row)


def _load(conn, cells, cases=(), name="wb.xlsx", sha=None):
    res = IngestionResult(cases=list(cases), cells=list(cells),
                          stats={"date_range": (min(c.work_date for c in cells),
                                                max(c.work_date for c in cells))})
    with conn.transaction():
        return loader.load_ingestion(PsycopgDB(conn), res, source_filename=name,
                                     triggered_by="hrbp:t", file_sha256=sha).run_id


def _active(conn, crm, day):
    rows = _q(conn, "select raw_value, bucket, ingestion_run_id from attendance.v_active_days "
                    "where crm_key = %s and work_date = %s", (crm.lower(), day))
    return rows[0] if rows else None


# ------------------------------------------------------------------ migration safety
def _parts():
    text = MIGRATION.read_text(encoding="utf-8").splitlines()
    marker = next(i for i, ln in enumerate(text) if ln.startswith("-- ROLLBACK"))
    forward = "\n".join(ln for ln in text[:marker] if ln.strip().lower() not in ("begin;", "commit;"))
    tail = [ln[3:] if ln.startswith("-- ") else "" for ln in text[marker:]]
    return forward, "\n".join(tail[tail.index("begin;") + 1:tail.index("commit;")])


def test_migration_refuses_to_run_twice(conn):
    forward, _ = _parts()
    with conn.cursor() as cur, pytest.raises(RaiseException, match="already exist"):
        cur.execute(forward)
    conn.rollback()


def test_migration_requires_phase0(conn):
    forward, rollback = _parts()
    with conn.cursor() as cur:
        cur.execute(rollback)
        cur.execute("alter table attendance.cases drop column hrbp_override_note")
        with pytest.raises(RaiseException, match="Phase 0"):
            cur.execute(forward)
    conn.rollback()


def test_rollback_then_reapply(conn):
    forward, rollback = _parts()
    with conn.cursor() as cur:
        cur.execute(rollback)
        cur.execute("select to_regclass('attendance.attendance_days'), "
                    "to_regclass('attendance.v_active_days')")
        assert cur.fetchone() == (None, None)
        cur.execute(forward)
        cur.execute("select to_regclass('attendance.attendance_days') is not null")
        assert cur.fetchone()[0]
    conn.rollback()


def test_sql_and_python_periods_agree_for_three_years(conn):
    rows = _q(conn, "select d, attendance.period_start(d) as s, attendance.period_end(d) as e "
                    "from (select date '2026-01-01' + i as d "
                    "      from generate_series(0, date '2028-12-31' - date '2026-01-01') i) x")
    assert len(rows) == 1096                                  # includes 2028-02-29
    assert all((r["s"], r["e"]) == attendance_period(r["d"]) for r in rows)


def test_uniqueness_and_blank_constraint(conn, org):
    run = _load(conn, [_cell("E-1", P, "Normal")])
    with pytest.raises(UniqueViolation), conn.cursor() as cur:
        cur.execute("insert into attendance.attendance_days (ingestion_run_id, crm, crm_key, "
                    "work_date, raw_value, bucket) values (%s, 'E-1', 'e-1', %s, 'X', 'skip')",
                    (run, P))
    conn.rollback()
    with pytest.raises(CheckViolation), conn.cursor() as cur:
        cur.execute("insert into attendance.attendance_days (ingestion_run_id, crm, crm_key, "
                    "work_date, raw_value, bucket) values (%s, 'E-1', 'e-1', %s, null, 'skip')",
                    (run, P + datetime.timedelta(days=1)))
    conn.rollback()


# ------------------------------------------------------------------ storage + active source
def test_cells_stored_with_identity_and_upload_metadata(conn, org):
    run = _load(conn, [_cell("E-1", P, "Normal"), _cell("E-X", P, "Absent", "trigger", tl=None)],
                sha="h1")
    rows = {r["crm_key"]: r for r in _q(conn, "select * from attendance.attendance_days")}
    assert rows["e-1"]["employee_id"] == org["E-1"]
    assert rows["e-1"]["manager_id_at_ingest"] == org["TL-A"]
    assert rows["e-x"]["employee_id"] is None                     # unmapped CRM kept, no TL
    meta = _q(conn, "select range_start, range_end, file_sha256, full_cells "
                    "from attendance.ingestion_runs where id = %s", (run,))[0]
    assert (meta["range_start"], meta["range_end"], meta["file_sha256"], meta["full_cells"]) == \
        (P, P, "h1", True)


def test_active_source_rules(conn, org):
    d1, d2, d3 = P, P + datetime.timedelta(days=1), P + datetime.timedelta(days=2)
    _load(conn, [_cell("E-1", d1, "Normal"), _cell("E-1", d2, "Late", "not_verified"),
                 _cell("E-1", d3, "Normal")], name="w1.xlsx", sha="a")
    r2 = _load(conn, [_cell("E-1", d1, "Absent", "trigger"),          # changed value
                      _cell("E-1", d2, None, "blank", None)],          # blank, d3 not covered
               name="w2.xlsx", sha="b")
    assert _active(conn, "E-1", d1)["raw_value"] == "Absent"
    assert _active(conn, "E-1", d2)["bucket"] == "blank"               # newer blank wins
    assert _active(conn, "E-1", d3)["raw_value"] == "Normal"           # partial upload keeps old
    r3 = _load(conn, [_cell("E-1", d1, "Absent", "trigger")], name="w2-again.xlsx", sha="b")
    assert _active(conn, "E-1", d1)["ingestion_run_id"] == r3 != r2    # same value, newer upload
    assert len(data.uploads_with_hash(conn, "b")) == 2                 # duplicate-file warning
    assert len(_q(conn, "select 1 from attendance.attendance_days")) == 6   # history kept


def test_cells_spanning_two_periods(conn, org):
    _load(conn, [_cell("E-1", D(2026, 10, 14), "Normal"), _cell("E-1", D(2026, 10, 15), "Late")])
    sep = {r["work_date"] for r in hrbp_period_attendance(conn, P) if r["coverage"] == "full"}
    octo = {r["work_date"] for r in hrbp_period_attendance(conn, D(2026, 10, 15))
            if r["coverage"] == "full"}
    assert sep == {D(2026, 10, 14)} and octo == {D(2026, 10, 15)}


# ------------------------------------------------------------------ remove / reset
def test_untouched_upload_removed_with_its_cells_history_still_protected(conn, org):
    untouched = _load(conn, [_cell("E-1", P, "Normal")], name="u.xlsx")
    assert data.remove_upload(conn, untouched) == {"cases_deleted": 0}
    assert _q(conn, "select 1 from attendance.attendance_days") == []
    case = CaseCandidate("E-1", "TL-A", P, "Absent")
    answered = _load(conn, [_cell("E-1", P, "Absent", "trigger")], cases=[case], name="a.xlsx")
    cid = _q(conn, "select id from attendance.cases")[0]["id"]
    assert data.submit_verdict(conn, cid, "absent", None, None, "tl:TL-A")
    assert data.remove_upload(conn, answered) == {"refused": 1}
    assert data.reset_all_cases(conn) == {"refused": 1}
    assert len(_q(conn, "select 1 from attendance.attendance_days")) == 1


def test_reset_clears_cells_when_nothing_has_history(conn, org):
    _load(conn, [_cell("E-1", P, "Normal")])
    assert data.reset_all_cases(conn) == {"cases_deleted": 0}
    assert _q(conn, "select 1 from attendance.attendance_days") == []


# ------------------------------------------------------------------ TL period contract
def _grid(rows):
    return {(r["employee_crm"], r["work_date"]): r for r in rows}


def test_tl_grid_scope_coverage_and_masking(conn, org):
    d1, d2 = P, P + datetime.timedelta(days=1)
    case = CaseCandidate("E-3", "TL-A", d2, "Absent")                 # E-3 is TL-B's employee
    _load(conn, [_cell("E-1", d1, "Sick Leave", canon="Sick Leave"),
                 _cell("E-1", d2, "Absent", "trigger"),
                 _cell("E-3", d2, "Absent", "trigger", tl="TL-B")], cases=[case])
    rows = tl_period_attendance(conn, org["TL-A"], P)
    crms = {r["employee_crm"] for r in rows}
    assert crms == {"E-1", "E-2", "E-3"}            # current team + E-3 via a TL-A case
    assert len(rows) == 3 * 30                       # every day of the 15 -> 14 period
    g = _grid(rows)
    assert g[("E-1", d1)]["status"] == "Leave"                         # Sick Leave masked
    assert "raw_value" not in g[("E-1", d1)] and "hrbp_override_note" not in g[("E-1", d1)]
    assert g[("E-1", d1)]["coverage"] == "full"
    assert g[("E-1", d2 + datetime.timedelta(days=5))]["coverage"] == "none"
    assert g[("E-3", d2)]["flagged"] and g[("E-3", d2)]["pending"]
    assert g[("E-2", d1)]["employment"] == "before_join"
    assert _grid(tl_period_attendance(conn, org["TL-B"], P)).keys() >= {("E-3", d2)}
    assert all(r["employee_crm"] == "E-3" for r in tl_period_attendance(conn, org["TL-B"], P))
    hrbp = _grid(hrbp_period_attendance(conn, P))
    assert hrbp[("E-1", d1)]["raw_value"] == "Sick Leave"              # HRBP sees exact value
    assert hrbp[("E-3", D(2026, 10, 2))]["employment"] == "after_exit"


def test_tl_grid_answered_overridden_and_flagged_only(conn, org):
    d1, d2, d3 = P, P + datetime.timedelta(days=1), P + datetime.timedelta(days=2)
    cases = [CaseCandidate("E-1", "TL-A", d, "Absent") for d in (d1, d2)]
    _load(conn, [_cell("E-1", d1, "Absent", "trigger"), _cell("E-1", d2, "Absent", "trigger")],
          cases=cases)
    ids = {r["work_date"]: r["id"] for r in _q(conn, "select id, work_date from attendance.cases")}
    assert data.submit_verdict(conn, ids[d1], "present", None, "ok", "tl:TL-A")
    assert data.submit_verdict(conn, ids[d2], "absent", None, None, "tl:TL-A")
    assert data.close_case(conn, ids[d2], "hrbp:x", final_status="sick_leave", comment="note")
    # a legacy, flagged-only case (no stored cells): an employee/date never loaded as cells
    _q(conn, "insert into attendance.cases (employee_id, manager_id, work_date, source_status) "
             "values (%s, %s, %s, 'No Show')", (org["E-1"], org["TL-A"], d3))
    g = _grid(tl_period_attendance(conn, org["TL-A"], P))
    assert g[("E-1", d1)]["tl_answer"] == "present" and not g[("E-1", d1)]["pending"]
    assert g[("E-1", d2)]["hrbp_overridden"] and g[("E-1", d2)]["hrbp_override_value"] == "leave"
    assert g[("E-1", d3)]["coverage"] == "flagged_only" and g[("E-1", d3)]["pending"]
    assert g[("E-1", d3)]["status"] == "No Show"


def test_source_changed_since_response(conn, org):
    d1, d2 = P, P + datetime.timedelta(days=1)
    cases = [CaseCandidate("E-1", "TL-A", d, "Absent") for d in (d1, d2)]
    _load(conn, [_cell("E-1", d1, "Absent", "trigger"), _cell("E-1", d2, "Absent", "trigger")],
          cases=cases, name="w1.xlsx")
    for cid in [r["id"] for r in _q(conn, "select id from attendance.cases")]:
        assert data.submit_verdict(conn, cid, "absent", None, None, "tl:TL-A")
    assert not any(r["source_changed_since_response"]
                   for r in tl_period_attendance(conn, org["TL-A"], P))
    # d1 re-flagged differently (case upsert + Phase 0 audit); d2 no longer flagged (no case run)
    _load(conn, [_cell("E-1", d1, "No Show", "trigger"), _cell("E-1", d2, "Normal")],
          cases=[CaseCandidate("E-1", "TL-A", d1, "No Show")], name="w2.xlsx")
    g = _grid(tl_period_attendance(conn, org["TL-A"], P))
    assert g[("E-1", d1)]["source_changed_since_response"]
    assert g[("E-1", d2)]["source_changed_since_response"]
    assert g[("E-1", d1)]["tl_answer"] == "absent"                     # answer never erased


def test_period_start_must_be_a_15th(conn, org):
    with pytest.raises(ValueError):
        tl_period_attendance(conn, org["TL-A"], D(2026, 9, 1))


def test_end_to_end_cross_year_workbook(conn, tmp_path):
    """Real path: workbook -> parse_reference/ingest_summary -> loaders -> TL contract."""
    import importlib.util

    import openpyxl
    spec = importlib.util.spec_from_file_location(   # the top-level tests/conftest.py fixtures
        "tests_conftest", Path(__file__).resolve().parents[1] / "conftest.py")
    top = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(top)
    _build_reference = top._build_reference
    from ingestion.reference import parse_reference
    from ingestion.summary import ingest_summary

    wb = openpyxl.Workbook()
    _build_reference(wb)
    sm = wb.create_sheet("Summary Report")
    sm.append(["Title"])
    sm.append([])
    days = [f"{d}-Dec" for d in range(15, 32)] + [f"{d:02d}-Jan" for d in range(1, 15)]
    sm.append(["CRM"] + days)
    sm.append(["E-1"] + ["Normal"] * 16 + ["Absent"] + ["Sick Leave"] + ["Normal"] * 13)
    path = tmp_path / "dec-jan.xlsx"
    wb.save(path)
    ref = parse_reference(str(path))
    res = ingest_summary(str(path), ref, year=2026)
    with conn.transaction():
        db = PsycopgDB(conn)
        loader.load_reference(db, ref)
        loader.load_ingestion(db, res, reference=ref, source_filename=path.name, file_sha256="x")
    tl_a = _q(conn, "select id from attendance.managers where crm = 'TL-A'")[0]["id"]
    g = _grid(tl_period_attendance(conn, tl_a, D(2026, 12, 15)))
    assert g[("E-1", D(2026, 12, 31))]["flagged"]                      # 31-Dec -> 2026
    assert g[("E-1", D(2027, 1, 1))]["status"] == "Leave"              # 01-Jan -> 2027, masked
    assert g[("E-1", D(2027, 1, 14))]["coverage"] == "full"
    assert _q(conn, "select work_date from attendance.cases")[0]["work_date"] == D(2026, 12, 31)


# ------------------------------------------------------------------ security fixes (review of PR #9)
def test_case_only_employee_visible_only_on_case_days(conn, org):
    d1, d2 = P, P + datetime.timedelta(days=1)
    # E-3 is on TL-B's team; TL-A gets visibility only because of the d2 case assigned to TL-A
    _load(conn, [_cell("E-3", d1, "Sick Leave", tl="TL-B"),
                 _cell("E-3", d2, "Absent", "trigger", tl="TL-B")],
          cases=[CaseCandidate("E-3", "TL-A", d2, "Absent")])
    g = _grid(tl_period_attendance(conn, org["TL-A"], P))
    other_day = g[("E-3", d1)]
    assert other_day["visibility"] == "case_only"
    assert other_day["status"] is None and other_day["bucket"] is None    # withheld
    assert other_day["coverage"] == "none" and other_day["case_id"] is None
    case_day = g[("E-3", d2)]
    assert case_day["status"] == "Absent" and case_day["pending"] and case_day["case_id"]
    assert all(r["visibility"] == "team" for r in tl_period_attendance(conn, org["TL-A"], P)
               if r["employee_crm"] != "E-3")
    # TL-B (E-3's own TL) still sees every day
    own = _grid(tl_period_attendance(conn, org["TL-B"], P))
    assert own[("E-3", d1)]["status"] == "Leave" and own[("E-3", d1)]["visibility"] == "team"


def test_unclassified_and_case_text_never_reach_the_tl_raw(conn, org):
    d1, d2 = P, P + datetime.timedelta(days=1)
    _load(conn, [_cell("E-1", d1, "Hospital visit, see doctor note", "unknown", canon=None)])
    _q(conn, "insert into attendance.cases (employee_id, manager_id, work_date, source_status) "
             "values (%s, %s, %s, 'Bereavement Leave - To Be Confirmed')",
       (org["E-1"], org["TL-A"], d2))                                  # flagged-only, raw text
    g = _grid(tl_period_attendance(conn, org["TL-A"], P))
    assert g[("E-1", d1)]["status"] == "Unclassified"
    assert g[("E-1", d2)]["coverage"] == "flagged_only" and g[("E-1", d2)]["status"] == "Leave"
    tl_text = repr(tl_period_attendance(conn, org["TL-A"], P))
    assert "Hospital" not in tl_text and "Bereavement" not in tl_text and "To Be Confirmed" not in tl_text
    hrbp = _grid(hrbp_period_attendance(conn, P))
    assert hrbp[("E-1", d1)]["raw_value"] == "Hospital visit, see doctor note"   # HRBP exact
