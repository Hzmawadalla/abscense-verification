"""Phase 1 upload safety against real PostgreSQL: parse -> preview writes nothing, a rejected or
failing upload leaves no partial state, and duplicate employee rows keep the active source, the case
text and source-change detection consistent without touching a TL's response or evidence."""
import datetime
import importlib.util
from pathlib import Path

import openpyxl
import pytest
from psycopg.rows import dict_row

from app import data, ingest_flow
from app.attendance_view import hrbp_period_attendance
from ingestion import loader
from ingestion.reference import parse_reference
from ingestion.summary import UnsafeWorkbookError, ingest_summary

D = datetime.date
MAY6 = D(2026, 5, 6)
PERIOD = D(2026, 4, 15)                       # the 15 Apr -> 14 May period containing 6 May
WRITE_TABLES = ("managers", "employees", "ingestion_runs", "cases", "ingestion_exceptions",
                "attendance_days", "audit_log", "case_attachments")

_spec = importlib.util.spec_from_file_location(
    "tests_conftest", Path(__file__).resolve().parents[1] / "conftest.py")
_top = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_top)


def _workbook(tmp_path, rows, headers=("06-May",), name="wb.xlsx"):
    wb = openpyxl.Workbook()
    _top._build_reference(wb)
    sm = wb.create_sheet("Summary Report")
    sm.append(["Title"])
    sm.append([])
    sm.append(["CRM", *headers])
    for r in rows:
        sm.append(r)
    path = tmp_path / name
    wb.save(path)
    return str(path)


def _counts(conn):
    with conn.cursor() as cur:
        out = {}
        for t in WRITE_TABLES:
            cur.execute(f"select count(*) from attendance.{t}")
            out[t] = cur.fetchone()[0]
    return out


def _upload(conn, path, sha):
    # End any read transaction first so each upload is its OWN transaction, as in the app
    # (autocommit connection): v_active_days orders uploads by their transaction timestamp.
    conn.commit()
    ref = parse_reference(path)
    res = ingest_summary(path, ref, year=2026)
    return ingest_flow.load_confirmed(conn, ref, res, Path(path).name, "hrbp:test", sha)


def _case(conn):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select id, source_status, manager_status, manager_comment, "
                    "manager_responded_at, status from attendance.cases")
        return cur.fetchall()


# ------------------------------------------------------------------ preview never writes
def test_parse_and_preview_write_nothing(conn, tmp_path):
    path = _workbook(tmp_path, [["E-1", "Absent"], ["E-2", "Normal"]])
    ref = parse_reference(path)
    res = ingest_summary(path, ref, year=2026)
    p = ingest_flow.build_preview(res, ref, data.uploads_with_hash(conn, "sha-x"))
    assert (p["first_date"], p["last_date"], p["cases"], p["cells"]) == (MAY6, MAY6, 1, 2)
    assert p["periods"] == [(PERIOD, D(2026, 5, 14))]
    assert all("Absent" not in line for line in ingest_flow.preview_lines(p))   # no cell text
    assert set(_counts(conn).values()) == {0}


def test_rejected_workbook_writes_nothing(conn, tmp_path):
    path = _workbook(tmp_path, [["E-1", "Absent", "Normal"]], headers=("06-May", "06-May"))
    with pytest.raises(UnsafeWorkbookError):
        ingest_summary(path, parse_reference(path), year=2026)
    assert set(_counts(conn).values()) == {0}


def test_a_failing_load_rolls_back_completely(conn, tmp_path, monkeypatch):
    path = _workbook(tmp_path, [["E-1", "Absent"], ["E-2", "Normal"]])
    # break the LAST statement of the load: the run, reference, cases and exceptions written before
    # it must all disappear with the transaction
    monkeypatch.setattr(loader, "INSERT_DAY", loader.INSERT_DAY.replace("attendance_days",
                                                                        "no_such_table"))
    with pytest.raises(Exception):
        _upload(conn, path, "sha-1")
    conn.rollback()
    assert set(_counts(conn).values()) == {0}


def test_preview_key_changes_with_file_or_year():
    k = ingest_flow.preview_key(b"ref", b"att", 2026)
    assert k == ingest_flow.preview_key(b"ref", b"att", 2026.0)
    assert k != ingest_flow.preview_key(b"ref", b"att2", 2026)
    assert k != ingest_flow.preview_key(b"ref", b"att", 2027)
    assert k != ingest_flow.preview_key(None, b"att", 2026)


# ------------------------------------------------------------------ L1 duplicate rows, end to end
def _grid_row(conn):
    rows = [r for r in hrbp_period_attendance(conn, PERIOD)
            if r["employee_crm"] == "E-1" and r["work_date"] == MAY6]
    assert len(rows) == 1
    return rows[0]


def test_duplicate_rows_before_tl_submission_case_matches_active_cell(conn, tmp_path):
    _upload(conn, _workbook(tmp_path, [["E-1", "Absent"], ["E-1", "No Show"]]), "s1")
    (case,) = _case(conn)
    r = _grid_row(conn)
    assert case["source_status"] == r["raw_value"] == "Absent"         # first row, both places
    assert not r["source_changed_since_response"]


def test_duplicate_rows_after_tl_submission_never_touch_the_response(conn, tmp_path):
    _upload(conn, _workbook(tmp_path, [["E-1", "Absent"]], name="w1.xlsx"), "s1")
    (case,) = _case(conn)
    assert data.submit_verdict(conn, case["id"], "absent", None, "called, no answer", "tl:TL-A")
    data.add_attachment(conn, case["id"], "cases/x/proof.png", "proof.png", "image/png", 10)
    before = _case(conn)[0]

    # same first row, a conflicting REPEATED row: nothing changes, no false "source changed"
    _upload(conn, _workbook(tmp_path, [["E-1", "Absent"], ["E-1", "Normal"]], name="w2.xlsx"), "s2")
    assert _case(conn)[0] == before
    assert not _grid_row(conn)["source_changed_since_response"]

    # the FIRST row really changes: source updated + flagged as changed, response kept intact
    _upload(conn, _workbook(tmp_path, [["E-1", "No Show"], ["E-1", "Absent"]], name="w3.xlsx"), "s3")
    after = _case(conn)[0]
    assert after["source_status"] == "No Show"
    assert {k: after[k] for k in ("manager_status", "manager_comment", "manager_responded_at",
                                  "status")} == \
           {k: before[k] for k in ("manager_status", "manager_comment", "manager_responded_at",
                                   "status")}
    r = _grid_row(conn)
    assert r["raw_value"] == "No Show" and r["source_changed_since_response"]
    with conn.cursor() as cur:
        cur.execute("select count(*) from attendance.case_attachments where voided_at is null")
        assert cur.fetchone()[0] == 1                                   # evidence untouched


def test_identical_repeated_date_is_stored_once_with_an_exception(conn, tmp_path):
    _upload(conn, _workbook(tmp_path, [["E-1", "Absent", "absent"]], headers=("06-May", "06-May")),
            "s1")
    with conn.cursor() as cur:
        cur.execute("select count(*) from attendance.attendance_days where crm_key = 'e-1'")
        assert cur.fetchone()[0] == 1
        cur.execute("select count(*) from attendance.ingestion_exceptions "
                    "where reason = 'duplicate_date_column' and work_date = %s", (MAY6,))
        assert cur.fetchone()[0] == 1
