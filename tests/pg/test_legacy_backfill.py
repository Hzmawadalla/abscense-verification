"""Legacy cell backfill against real PostgreSQL: cells are attached to the EXISTING upload, every
refusal writes nothing, and no verification/history data changes — ever."""
import datetime
import hashlib
import importlib.util
import shutil
from pathlib import Path

import openpyxl
import pytest
from psycopg.rows import dict_row

from app import data
from app.attendance_view import hrbp_period_attendance, tl_period_attendance
from ingestion import backfill, loader
from ingestion.backfill import BackfillRefused, apply_backfill, plan_backfill
from ingestion.db_psycopg import PsycopgDB
from ingestion.reference import parse_reference
from ingestion.summary import ingest_summary

D = datetime.date
PERIOD = D(2026, 4, 15)
HEADERS = ("06-May", "07-May", "08-May")
ROWS = [["E-1", "Absent", "Normal", "Sick Leave"],
        ["E-2", "Normal", "No Show (HD)", "Normal"],
        ["E-UNKNOWN", "Absent", "Normal", "Normal"]]
HISTORY_TABLES = ("managers", "employees", "cases", "case_attachments", "audit_log",
                  "ingestion_exceptions", "notifications")

_spec = importlib.util.spec_from_file_location(
    "tests_conftest", Path(__file__).resolve().parents[1] / "conftest.py")
_top = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_top)


def _workbook(path, rows=ROWS, headers=HEADERS):
    wb = openpyxl.Workbook()
    _top._build_reference(wb)
    sm = wb.create_sheet("Summary Report")
    sm.append(["Title"])
    sm.append([])
    sm.append(["CRM", *headers])
    for r in rows:
        sm.append(r)
    wb.save(path)
    return Path(path)


def _legacy_upload(conn, path):
    """Load a workbook exactly as the pre-Phase-1 app did: run + reference + cases + exceptions,
    no cells, no hash, no date range, full_cells = false."""
    conn.commit()
    ref = parse_reference(str(path))
    res = ingest_summary(str(path), ref, year=2026)
    res.cells = []
    res.stats = {}
    with conn.transaction():
        db = PsycopgDB(conn)
        loader.load_reference(db, ref)
        return loader.load_ingestion(db, res, reference=ref, source_filename=path.name,
                                     triggered_by="hrbp:legacy").run_id


def _phase1_upload(conn, path):
    conn.commit()
    ref = parse_reference(str(path))
    res = ingest_summary(str(path), ref, year=2026)
    with conn.transaction():
        db = PsycopgDB(conn)
        loader.load_reference(db, ref)
        return loader.load_ingestion(db, res, reference=ref, source_filename=path.name,
                                     triggered_by="hrbp:new", file_sha256="x").run_id


def _q(conn, sql, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _snapshot(conn, run_id):
    """Everything a backfill must never change, row for row."""
    snap = {t: [tuple(r.values()) for r in _q(conn, f"select * from attendance.{t} order by id")]
            for t in HISTORY_TABLES}
    snap["other_runs"] = [tuple(r.values()) for r in _q(
        conn, "select * from attendance.ingestion_runs where id <> %s order by id", (run_id,))]
    snap["run_fixed"] = [tuple(r.values()) for r in _q(
        conn, "select id, source_filename, triggered_by, created_count, skipped_count, "
              "exception_count, created_at from attendance.ingestion_runs where id = %s", (run_id,))]
    snap["other_cells"] = [tuple(r.values()) for r in _q(
        conn, "select * from attendance.attendance_days where ingestion_run_id <> %s order by id",
        (run_id,))]
    return snap


def _cells(conn, run_id):
    return _q(conn, "select count(*) as n from attendance.attendance_days where ingestion_run_id = %s",
              (run_id,))[0]["n"]


def _run_row(conn, run_id):
    return _q(conn, "select full_cells, file_sha256, range_start, range_end "
                    "from attendance.ingestion_runs where id = %s", (run_id,))[0]


@pytest.fixture()
def legacy(conn, tmp_path):
    """A legacy upload with real history: a TL answer + comment + evidence on one case and an HRBP
    override on the other."""
    path = _workbook(tmp_path / "Attendance_Report_legacy.xlsx")
    run_id = _legacy_upload(conn, path)
    cases = {(r["crm"], r["work_date"]): r["id"] for r in _q(
        conn, "select e.crm, c.work_date, c.id from attendance.cases c "
              "join attendance.employees e on e.id = c.employee_id")}
    a, b = cases[("E-1", D(2026, 5, 6))], cases[("E-2", D(2026, 5, 7))]
    assert data.submit_verdict(conn, a, "present", None, "was on calls", "tl:TL-A")
    data.add_attachment(conn, a, "cases/a/proof.png", "proof.png", "image/png", 10)
    assert data.close_case(conn, b, "hrbp:x", final_status="sick_leave", comment="hrbp note")
    conn.commit()
    return {"run_id": run_id, "path": path, "cases": cases}


# ------------------------------------------------------------------ success
def test_backfill_attaches_cells_to_the_existing_upload_and_changes_nothing_else(conn, legacy):
    run_id = legacy["run_id"]
    runs_before = _q(conn, "select count(*) as n from attendance.ingestion_runs")[0]["n"]
    before = _snapshot(conn, run_id)

    plan = plan_backfill(conn, run_id, legacy["path"], 2026)
    assert plan.cases_checked == 2 and (plan.distinct_crms, plan.distinct_dates) == (3, 3)
    assert apply_backfill(conn, plan) == 9
    conn.commit()

    assert _snapshot(conn, run_id) == before                  # no history touched, row for row
    assert _q(conn, "select count(*) as n from attendance.ingestion_runs")[0]["n"] == runs_before
    run = _run_row(conn, run_id)
    assert run["full_cells"] and run["file_sha256"] == hashlib.sha256(
        legacy["path"].read_bytes()).hexdigest()
    assert (run["range_start"], run["range_end"]) == (D(2026, 5, 6), D(2026, 5, 8))
    cells = _q(conn, "select crm_key, employee_id is not null as mapped, manager_id_at_ingest "
                     "from attendance.attendance_days where ingestion_run_id = %s", (run_id,))
    assert len(cells) == 9 and all(c["manager_id_at_ingest"] is None for c in cells)
    assert {c["crm_key"]: c["mapped"] for c in cells} == {"e-1": True, "e-2": True,
                                                         "e-unknown": False}


def test_backfilled_period_shows_cells_and_missing_days_stay_missing(conn, legacy):
    apply_backfill(conn, plan_backfill(conn, legacy["run_id"], legacy["path"], 2026))
    conn.commit()
    tl_a = _q(conn, "select id from attendance.managers where crm = 'TL-A'")[0]["id"]
    g = {(r["employee_crm"], r["work_date"]): r for r in tl_period_attendance(conn, tl_a, PERIOD)}
    assert g[("E-1", D(2026, 5, 8))]["status"] == "Leave" and g[("E-1", D(2026, 5, 8))]["coverage"] == "full"
    assert g[("E-1", D(2026, 5, 6))]["tl_answer"] == "Present"           # answer still there
    other = g[("E-1", D(2026, 5, 1))]                                      # not in the workbook
    assert other["coverage"] == "none" and other["status"] is None        # missing, never Absent


# ------------------------------------------------------------------ active-source precedence
def test_newer_upload_stays_active_on_overlapping_dates(conn, legacy, tmp_path):
    newer = _workbook(tmp_path / "newer.xlsx", rows=[["E-1", "Late", "Late"]],
                      headers=("07-May", "08-May"))
    new_run = _phase1_upload(conn, newer)
    apply_backfill(conn, plan_backfill(conn, legacy["run_id"], legacy["path"], 2026))
    conn.commit()
    active = {r["work_date"]: (r["ingestion_run_id"], r["raw_value"]) for r in _q(
        conn, "select work_date, ingestion_run_id, raw_value from attendance.v_active_days "
              "where crm_key = 'e-1'")}
    assert active[D(2026, 5, 6)] == (legacy["run_id"], "Absent")          # only the legacy file
    assert active[D(2026, 5, 7)] == (new_run, "Late")                     # newer upload wins
    assert active[D(2026, 5, 8)] == (new_run, "Late")


def test_original_text_is_taken_from_the_audit_when_a_later_upload_changed_the_case(
        conn, legacy, tmp_path):
    # a later upload re-flags E-2 07-May differently: the case's source text changes (audited)
    later = _workbook(tmp_path / "later.xlsx", rows=[["E-2", "Absent"]], headers=("07-May",))
    _phase1_upload(conn, later)
    case = _q(conn, "select source_status from attendance.cases where id = %s",
              (legacy["cases"][("E-2", D(2026, 5, 7))],))[0]
    assert case["source_status"] == "Absent"
    plan = plan_backfill(conn, legacy["run_id"], legacy["path"], 2026)   # still verifies
    assert plan.cases_checked == 2


# ------------------------------------------------------------------ refusals write nothing
def _expect_refusal(conn, legacy, path, run_id=None, year=2026, match=""):
    run_id = run_id or legacy["run_id"]
    before = _snapshot(conn, legacy["run_id"])
    run_before = _run_row(conn, legacy["run_id"])
    cells_before = _q(conn, "select count(*) as n from attendance.attendance_days")[0]["n"]
    with pytest.raises(BackfillRefused, match=match):
        plan_backfill(conn, run_id, path, year)
    conn.rollback()
    assert _snapshot(conn, legacy["run_id"]) == before
    assert _run_row(conn, legacy["run_id"]) == run_before
    assert _q(conn, "select count(*) as n from attendance.attendance_days")[0]["n"] == cells_before


def test_refuses_a_workbook_with_a_different_flagged_value(conn, legacy, tmp_path):
    rows = [list(r) for r in ROWS]
    rows[0][1] = "No Show"                                    # E-1 06-May differs from its case
    path = _workbook(tmp_path / legacy["path"].name, rows=rows)
    _expect_refusal(conn, legacy, path, match="do not match")


def test_refuses_a_workbook_missing_a_flagged_day(conn, legacy, tmp_path):
    path = _workbook(tmp_path / legacy["path"].name, rows=ROWS, headers=("06-May", "08-May", "09-May"))
    _expect_refusal(conn, legacy, path, match="do not match")


def test_refuses_the_wrong_year(conn, legacy):
    _expect_refusal(conn, legacy, legacy["path"], year=2027, match="do not match")


def test_refuses_a_renamed_file(conn, legacy, tmp_path):
    renamed = tmp_path / "renamed.xlsx"
    shutil.copy(legacy["path"], renamed)
    _expect_refusal(conn, legacy, renamed, match="file name")


def test_refuses_an_ambiguous_workbook(conn, legacy, tmp_path):
    path = _workbook(tmp_path / legacy["path"].name, rows=[["E-1", "Absent", "Normal"]],
                     headers=("06-May", "06-May"))                 # conflicting repeated date
    _expect_refusal(conn, legacy, path, match="parsed safely")


def test_refuses_an_unknown_upload(conn, legacy):
    _expect_refusal(conn, legacy, legacy["path"], run_id="00000000-0000-0000-0000-000000000000",
                    match="no upload")


def test_refuses_an_upload_that_created_no_cases(conn, legacy):
    """The real duplicate re-uploads (same file seconds later) created no cases: unverifiable."""
    dup = _legacy_upload(conn, legacy["path"])
    assert _q(conn, "select count(*) as n from attendance.cases where ingestion_run_id = %s",
              (dup,))[0]["n"] == 0
    _expect_refusal(conn, legacy, legacy["path"], run_id=dup, match="no cases")
    assert _cells(conn, dup) == 0


def test_refuses_an_upload_that_already_has_cells(conn, legacy, tmp_path):
    new_run = _phase1_upload(conn, _workbook(tmp_path / "p1.xlsx"))
    _expect_refusal(conn, legacy, tmp_path / "p1.xlsx", run_id=new_run, match="already has")


def test_rerun_is_refused_and_writes_nothing(conn, legacy):
    plan = plan_backfill(conn, legacy["run_id"], legacy["path"], 2026)
    apply_backfill(conn, plan)
    conn.commit()
    after_first = _snapshot(conn, legacy["run_id"])
    cells = _cells(conn, legacy["run_id"])
    with pytest.raises(BackfillRefused, match="already has"):
        plan_backfill(conn, legacy["run_id"], legacy["path"], 2026)
    with pytest.raises(BackfillRefused, match="changed since"):
        apply_backfill(conn, plan)                            # a stale plan re-applied
    conn.rollback()
    assert _cells(conn, legacy["run_id"]) == cells and _snapshot(conn, legacy["run_id"]) == after_first


def test_a_failure_mid_write_rolls_back_completely(conn, legacy, monkeypatch):
    plan = plan_backfill(conn, legacy["run_id"], legacy["path"], 2026)
    before, run_before = _snapshot(conn, legacy["run_id"]), _run_row(conn, legacy["run_id"])
    monkeypatch.setattr(backfill, "_MARK_RUN_SQL", backfill._MARK_RUN_SQL.replace(
        "ingestion_runs", "no_such_table"))                  # fails AFTER the cells were inserted
    with pytest.raises(Exception):
        apply_backfill(conn, plan)
    conn.rollback()
    assert _cells(conn, legacy["run_id"]) == 0
    assert _snapshot(conn, legacy["run_id"]) == before and _run_row(conn, legacy["run_id"]) == run_before


# ------------------------------------------------------------------ the operator tool
def test_tool_dry_run_writes_nothing_and_prints_no_cell_data(conn, legacy, pg_dsn):
    from tools.backfill_cells import run
    lines = []
    conn.commit()
    assert run(pg_dsn, legacy["run_id"], legacy["path"], 2026, out=lines.append) == 0
    text = "\n".join(lines)
    assert "DRY RUN" in text and "9 cell(s) for 3 CRM(s) x 3 date(s)" in text
    for secret in ("E-1", "E-2", "E-UNKNOWN", "Absent", "Sick", "No Show"):
        assert secret not in text
    assert _cells(conn, legacy["run_id"]) == 0 and not _run_row(conn, legacy["run_id"])["full_cells"]


def test_tool_apply_needs_the_reviewed_hash(conn, legacy, pg_dsn):
    from tools.backfill_cells import run
    lines = []
    conn.commit()
    assert run(pg_dsn, legacy["run_id"], legacy["path"], 2026, apply=True,
               expect_sha256="0" * 64, out=lines.append) == 2
    assert "REFUSED" in lines[-1] and _cells(conn, legacy["run_id"]) == 0
    sha = hashlib.sha256(legacy["path"].read_bytes()).hexdigest()
    assert run(pg_dsn, legacy["run_id"], legacy["path"], 2026, apply=True, expect_sha256=sha,
               out=lines.append) == 0
    assert lines[-1].startswith("WRITTEN: 9 cell(s)") and _cells(conn, legacy["run_id"]) == 9


def test_tool_reports_refusals_and_needs_the_dsn_from_the_environment(conn, legacy, pg_dsn,
                                                                     tmp_path, monkeypatch):
    from tools import backfill_cells
    renamed = tmp_path / "renamed.xlsx"
    shutil.copy(legacy["path"], renamed)
    lines = []
    conn.commit()
    assert backfill_cells.run(pg_dsn, legacy["run_id"], renamed, 2026, out=lines.append) == 2
    assert lines == ["REFUSED: the workbook file name does not match the upload's recorded file "
                     "name. Nothing was written."]
    sha = hashlib.sha256(legacy["path"].read_bytes()).hexdigest()
    apply_backfill(conn, plan_backfill(conn, legacy["run_id"], legacy["path"], 2026))
    conn.commit()
    lines.clear()
    assert backfill_cells.run(pg_dsn, legacy["run_id"], legacy["path"], 2026, apply=True,
                              expect_sha256=sha, out=lines.append) == 2       # rerun refused
    assert lines[-1].startswith("REFUSED: this upload already has attendance cells")
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    with pytest.raises(SystemExit, match="environment only"):
        backfill_cells.main([str(legacy["path"]), "--run-id", str(legacy["run_id"]), "--year", "2026"])
