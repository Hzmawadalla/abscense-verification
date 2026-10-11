"""Legacy cell backfill against real PostgreSQL: cells are attached to the EXISTING upload, every
refusal writes nothing, and no verification/history data changes — ever."""
import dataclasses
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


def _plan(conn, run_id, path, year=2026):
    path = Path(path)
    return plan_backfill(conn, run_id, path.read_bytes(), path.name, year)


def _apply(conn, plan):
    return apply_backfill(conn, plan, plan.file_sha256)


def _x(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)


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

    plan = _plan(conn, run_id, legacy["path"], 2026)
    assert plan.cases_checked == 2 and (plan.distinct_crms, plan.distinct_dates) == (3, 3)
    assert _apply(conn, plan) == 9
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
    _apply(conn, _plan(conn, legacy["run_id"], legacy["path"], 2026))
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
    _apply(conn, _plan(conn, legacy["run_id"], legacy["path"], 2026))
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
    plan = _plan(conn, legacy["run_id"], legacy["path"], 2026)   # still verifies
    assert plan.cases_checked == 2


# ------------------------------------------------------------------ refusals write nothing
def _expect_refusal(conn, legacy, path, run_id=None, year=2026, match=""):
    run_id = run_id or legacy["run_id"]
    before = _snapshot(conn, legacy["run_id"])
    run_before = _run_row(conn, legacy["run_id"])
    cells_before = _q(conn, "select count(*) as n from attendance.attendance_days")[0]["n"]
    with pytest.raises(BackfillRefused, match=match):
        _plan(conn, run_id, path, year)
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
    plan = _plan(conn, legacy["run_id"], legacy["path"], 2026)
    _apply(conn, plan)
    conn.commit()
    after_first = _snapshot(conn, legacy["run_id"])
    cells = _cells(conn, legacy["run_id"])
    with pytest.raises(BackfillRefused, match="already has"):
        _plan(conn, legacy["run_id"], legacy["path"], 2026)
    with pytest.raises(BackfillRefused, match="re-check under lock failed"):
        _apply(conn, plan)                            # a stale plan re-applied
    conn.rollback()
    assert _cells(conn, legacy["run_id"]) == cells and _snapshot(conn, legacy["run_id"]) == after_first


def test_a_failure_mid_write_rolls_back_completely(conn, legacy, monkeypatch):
    plan = _plan(conn, legacy["run_id"], legacy["path"], 2026)
    before, run_before = _snapshot(conn, legacy["run_id"]), _run_row(conn, legacy["run_id"])
    monkeypatch.setattr(backfill, "_MARK_RUN_SQL", backfill._MARK_RUN_SQL.replace(
        "ingestion_runs", "no_such_table"))                  # fails AFTER the cells were inserted
    with pytest.raises(Exception):
        _apply(conn, plan)
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
    _apply(conn, _plan(conn, legacy["run_id"], legacy["path"], 2026))
    conn.commit()
    lines.clear()
    assert backfill_cells.run(pg_dsn, legacy["run_id"], legacy["path"], 2026, apply=True,
                              expect_sha256=sha, out=lines.append) == 2       # rerun refused
    assert lines[-1].startswith("REFUSED: this upload already has attendance cells")
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    with pytest.raises(SystemExit, match="environment only"):
        backfill_cells.main([str(legacy["path"]), "--run-id", str(legacy["run_id"]), "--year", "2026"])



# ------------------------------------------------------------------ M2: approval bound to bytes
def test_tool_refuses_a_file_replaced_after_its_hash_was_approved(conn, legacy, pg_dsn):
    from tools.backfill_cells import run
    approved = hashlib.sha256(legacy["path"].read_bytes()).hexdigest()       # file A, reviewed
    rows = [list(r) for r in ROWS]
    rows[1][1] = "Late"                                                      # file B, same name
    _workbook(legacy["path"], rows=rows)
    lines = []
    conn.commit()
    assert run(pg_dsn, legacy["run_id"], legacy["path"], 2026, apply=True,
               expect_sha256=approved, out=lines.append) == 2
    assert "REFUSED" in lines[-1] and _cells(conn, legacy["run_id"]) == 0
    assert not _run_row(conn, legacy["run_id"])["full_cells"]


def test_apply_writes_only_the_approved_bytes(conn, legacy, tmp_path):
    plan = _plan(conn, legacy["run_id"], legacy["path"])
    approved = plan.file_sha256
    other = _workbook(tmp_path / "other.xlsx", rows=[["E-1", "Absent", "Late", "Late"]])
    with pytest.raises(BackfillRefused, match="approved sha256"):          # wrong approval
        apply_backfill(conn, plan, hashlib.sha256(other.read_bytes()).hexdigest())
    swapped = dataclasses.replace(plan, data=other.read_bytes())           # bytes swapped in plan
    with pytest.raises(BackfillRefused, match="approved sha256"):
        apply_backfill(conn, swapped, approved)
    with pytest.raises(BackfillRefused, match="approved sha256"):
        apply_backfill(conn, plan, "")
    conn.rollback()
    assert _cells(conn, legacy["run_id"]) == 0
    edited = dataclasses.replace(plan, cells=plan.cells[:2])               # cells edited in plan
    assert apply_backfill(conn, edited, approved) == 9                     # re-parsed, not trusted
    conn.commit()
    assert _cells(conn, legacy["run_id"]) == 9
    assert _run_row(conn, legacy["run_id"])["file_sha256"] == approved


# ------------------------------------------------------------------ M1: both directions + dates
def test_refuses_a_larger_workbook_with_unsupported_flagged_dates(conn, legacy, tmp_path):
    rows = [r + ["Absent", "Normal"] for r in ROWS]                 # 09-May flagged for everyone
    path = _workbook(tmp_path / legacy["path"].name, rows=rows,
                     headers=HEADERS + ("09-May", "10-May"))
    _expect_refusal(conn, legacy, path, match="no matching case or exception")


def test_refuses_an_extra_flagged_entry_inside_the_range(conn, legacy, tmp_path):
    rows = [list(r) for r in ROWS]
    rows[1][1] = "Absent"                         # E-2 06-May flagged: no case, no exception
    path = _workbook(tmp_path / legacy["path"].name, rows=rows)
    _expect_refusal(conn, legacy, path, match="1 flagged cell")


def test_refuses_a_half_day_mismatch(conn, legacy):
    _x(conn, "update attendance.cases set is_half_day = false where id = %s",
       (legacy["cases"][("E-2", D(2026, 5, 7))],))
    conn.commit()
    _expect_refusal(conn, legacy, legacy["path"], match="do not match")


def test_refuses_dates_after_the_upload_date(conn, legacy):
    _x(conn, "update attendance.ingestion_runs set created_at = '2026-05-07 12:00+03' "
             "where id = %s", (legacy["run_id"],))
    conn.commit()
    _expect_refusal(conn, legacy, legacy["path"], match="later than the upload's own date")


def test_upload_date_uses_the_business_time_zone(conn, legacy):
    # 2026-05-07 22:30 UTC is already 2026-05-08 in Cairo (UTC+3 in May): 08-May is not a future date
    _x(conn, "update attendance.ingestion_runs set created_at = '2026-05-07 22:30+00' "
             "where id = %s", (legacy["run_id"],))
    conn.commit()
    assert _plan(conn, legacy["run_id"], legacy["path"]).cases_checked == 2


def test_valid_workbook_with_unflagged_days_outside_the_evidence_span(conn, tmp_path):
    rows = [["E-1", "Normal", "Absent", "Normal", "Normal", "Normal"],
            ["E-2", "Normal", "Normal", "No Show", "Normal", "Normal"]]
    path = _workbook(tmp_path / "wide.xlsx", rows=rows,
                     headers=("05-May", "06-May", "07-May", "08-May", "09-May"))
    run_id = _legacy_upload(conn, path)
    plan = _plan(conn, run_id, path)
    assert plan.cases_checked == 2 and len(plan.cells) == 10
    assert plan.dates_outside_evidence == [D(2026, 5, 5), D(2026, 5, 8), D(2026, 5, 9)]


def test_exception_backed_flagged_cells_are_counted(conn, legacy):
    plan = _plan(conn, legacy["run_id"], legacy["path"])
    assert plan.exceptions_matched == 1                                # E-UNKNOWN 06-May


def _overlap_setup(conn, tmp_path, audited):
    """An EARLIER upload creates E-1 06-May; the TARGET upload re-flags it (same text, so no new
    case) and creates E-2 07-May itself. `audited` first records one source change so that source
    auditing is provably on before both uploads."""
    if audited:
        _legacy_upload(conn, _workbook(tmp_path / "a0.xlsx", rows=[["E-1", "Absent"]],
                                       headers=("01-May",)))
        _legacy_upload(conn, _workbook(tmp_path / "a1.xlsx", rows=[["E-1", "No Show"]],
                                       headers=("01-May",)))           # audited source change
    _legacy_upload(conn, _workbook(tmp_path / "earlier.xlsx", rows=[["E-1", "Absent"]],
                                   headers=("06-May",)))
    target = _workbook(tmp_path / "target.xlsx",
                       rows=[["E-1", "Absent", "Normal"], ["E-2", "Normal", "No Show"]],
                       headers=("06-May", "07-May"))
    return _legacy_upload(conn, target), target


def test_overlap_with_an_earlier_case_passes_when_history_is_proven(conn, tmp_path):
    run_id, target = _overlap_setup(conn, tmp_path, audited=True)
    assert _q(conn, "select count(*) as n from attendance.cases where ingestion_run_id = %s",
              (run_id,))[0]["n"] == 1
    plan = _plan(conn, run_id, target)
    assert plan.cases_checked == 1 and plan.overlaps_supported == 1


def test_overlap_without_audit_history_is_refused_with_zero_writes(conn, tmp_path):
    run_id, target = _overlap_setup(conn, tmp_path, audited=False)
    before = _q(conn, "select count(*) as n from attendance.attendance_days")[0]["n"]
    with pytest.raises(BackfillRefused, match="cannot be proven"):
        _plan(conn, run_id, target)
    conn.rollback()
    assert _q(conn, "select count(*) as n from attendance.attendance_days")[0]["n"] == before
    assert not _run_row(conn, run_id)["full_cells"]


def test_overlap_with_a_different_earlier_text_is_refused(conn, tmp_path):
    run_id, _ = _overlap_setup(conn, tmp_path, audited=True)
    edited = _workbook(tmp_path / "target.xlsx",
                       rows=[["E-1", "No Show", "Normal"], ["E-2", "Normal", "No Show"]],
                       headers=("06-May", "07-May"))
    with pytest.raises(BackfillRefused, match="no matching case or exception"):
        _plan(conn, run_id, edited)


# ------------------------------------------------------------------ CLI hardening + transactions
def test_dry_run_session_rejects_writes(conn, legacy, pg_dsn, monkeypatch):
    import psycopg
    from tools import backfill_cells
    seen = []

    def try_to_write(c, *args, **kwargs):
        try:
            c.execute("insert into attendance.notifications (channel) values ('email')")
        except psycopg.errors.ReadOnlySqlTransaction:
            seen.append("rejected")
        raise BackfillRefused("probe finished")

    monkeypatch.setattr(backfill_cells, "plan_backfill", try_to_write)
    conn.commit()
    lines = []
    assert backfill_cells.run(pg_dsn, legacy["run_id"], legacy["path"], 2026, out=lines.append) == 2
    assert seen == ["rejected"]
    assert _q(conn, "select count(*) as n from attendance.notifications")[0]["n"] == 0


def test_two_concurrent_applies_write_once(conn, legacy, pg_dsn):
    import threading

    import psycopg
    plan = _plan(conn, legacy["run_id"], legacy["path"])
    conn.commit()
    barrier, results = threading.Barrier(2), []

    def worker():
        with psycopg.connect(pg_dsn) as c:
            barrier.wait()
            try:
                results.append(apply_backfill(c, plan, plan.file_sha256))
            except BackfillRefused as e:
                results.append(str(e))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(results) == 2
    assert results.count(9) == 1
    assert any(isinstance(r, str) and "re-check under lock failed" in r for r in results)
    assert _cells(conn, legacy["run_id"]) == 9


def test_cli_rejects_a_malformed_run_id_without_connecting(legacy):
    from tools.backfill_cells import run
    lines = []
    assert run("postgresql://nobody@203.0.113.1:1/none", "not-a-uuid", legacy["path"], 2026,
               out=lines.append) == 2
    assert lines == ["REFUSED: --run-id is not a valid upload id. Nothing was written."]


def test_cli_connection_failure_is_sanitized(legacy):
    from tools.backfill_cells import run
    lines = []
    assert run("postgresql://secretuser:secretpass@127.0.0.1:1/db", legacy["run_id"],
               legacy["path"], 2026, out=lines.append) == 3
    text = "\n".join(lines)
    assert text.startswith("ERROR: database error (") and "Nothing was written" in text
    for leak in ("secretuser", "secretpass", "127.0.0.1", "postgresql://"):
        assert leak not in text


def test_cli_missing_file_is_reported_without_its_path(legacy, tmp_path):
    from tools.backfill_cells import run
    lines = []
    missing = tmp_path / "private-folder" / "nope.xlsx"
    assert run("postgresql://x@127.0.0.1:1/db", legacy["run_id"], missing, 2026,
               out=lines.append) == 3
    assert lines == ["ERROR: the workbook file could not be read (FileNotFoundError). "
                     "Nothing was written."]


def test_cli_expect_project_ref_guard(conn, legacy, pg_dsn):
    from tools.backfill_cells import run
    lines = []
    conn.commit()
    assert run(pg_dsn, legacy["run_id"], legacy["path"], 2026, out=lines.append,
               expect_project_ref="twddavxodczywjshedqk") == 2
    assert lines == ["REFUSED: the connection does not target the expected project. "
                     "Nothing was written."]
    assert run(pg_dsn, legacy["run_id"], legacy["path"], 2026, out=lines.append,
               expect_project_ref="127.0.0.1") == 0                    # optional; local works
    assert _cells(conn, legacy["run_id"]) == 0


def test_cli_never_prints_a_traceback(monkeypatch, capsys, legacy):
    from tools import backfill_cells

    def boom(*a, **k):
        raise RuntimeError("postgresql://user:pw@host/db exploded")

    monkeypatch.setattr(backfill_cells, "run", boom)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://user:pw@host/db")
    with pytest.raises(SystemExit) as e:
        backfill_cells.main([str(legacy["path"]), "--run-id", str(legacy["run_id"]),
                             "--year", "2026"])
    assert e.value.code == 3
    out = capsys.readouterr()
    assert "RuntimeError" in out.out and "postgresql://" not in out.out + out.err
    assert "Traceback" not in out.out + out.err


def test_cli_help_description_is_complete():
    from tools.backfill_cells import DESCRIPTION
    assert DESCRIPTION.endswith("ingestion/backfill.py for exactly what is and is not written).")


def test_cli_dry_run_reports_evidence_without_personal_data(conn, legacy, pg_dsn):
    from tools.backfill_cells import run
    lines = []
    conn.commit()
    assert run(pg_dsn, legacy["run_id"], legacy["path"], 2026, out=lines.append) == 0
    assert lines[1] == ("evidence: 1 flagged cell(s) matched to this upload's exceptions, 0 to "
                        "earlier uploads' cases; dates outside the case/exception span (unflagged, "
                        "reported not refused): 2026-05-08")


# ------------------------------------------------------------------ M1: text "at the time" from the audit
def _audit_on(conn, tmp_path):
    _legacy_upload(conn, _workbook(tmp_path / "a0.xlsx", rows=[["E-2", "Absent"]], headers=("01-May",)))
    _legacy_upload(conn, _workbook(tmp_path / "a1.xlsx", rows=[["E-2", "No Show"]], headers=("01-May",)))


def _target(conn, tmp_path, e1_text):
    path = _workbook(tmp_path / "target.xlsx",
                     rows=[["E-1", e1_text, "Normal"], ["E-2", "Normal", "No Show"]],
                     headers=("06-May", "07-May"))
    return _legacy_upload(conn, path), path


def test_overlap_uses_the_text_before_a_later_change(conn, tmp_path):
    """Earlier case 'Absent'; the target upload agreed; a LATER upload changed it to 'No Show'.
    At the target's time the case said 'Absent' (the first audited change's old value)."""
    _audit_on(conn, tmp_path)
    _legacy_upload(conn, _workbook(tmp_path / "earlier.xlsx", rows=[["E-1", "Absent"]],
                                   headers=("06-May",)))
    run_id, path = _target(conn, tmp_path, "Absent")
    _legacy_upload(conn, _workbook(tmp_path / "later.xlsx", rows=[["E-1", "No Show"]],
                                   headers=("06-May",)))                # audited change after T
    assert _plan(conn, run_id, path).overlaps_supported == 1


def test_overlap_uses_the_text_after_an_earlier_change(conn, tmp_path):
    """Earlier case 'Absent', changed to 'No Show' BEFORE the target upload: the target's sheet
    must say 'No Show' (the latest audited change at or before its time)."""
    _audit_on(conn, tmp_path)
    _legacy_upload(conn, _workbook(tmp_path / "earlier.xlsx", rows=[["E-1", "Absent"]],
                                   headers=("06-May",)))
    _legacy_upload(conn, _workbook(tmp_path / "middle.xlsx", rows=[["E-1", "No Show"]],
                                   headers=("06-May",)))                # audited change before T
    run_id, path = _target(conn, tmp_path, "No Show")
    assert _plan(conn, run_id, path).overlaps_supported == 1
    stale = _workbook(tmp_path / "target.xlsx",
                      rows=[["E-1", "Absent", "Normal"], ["E-2", "Normal", "No Show"]],
                      headers=("06-May", "07-May"))                     # the pre-change text
    with pytest.raises(BackfillRefused, match="no matching case or exception"):
        _plan(conn, run_id, stale)
