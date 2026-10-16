"""D4: a sheet CRM that HC/Structure doesn't know, with no flagged day, is reported — not silently
dropped, and never turned into an absence or a case."""
import importlib.util
from pathlib import Path

import openpyxl
import pytest

from app import ingest_flow
from ingestion.reference import parse_reference
from ingestion.summary import UNMAPPED_NO_FLAGS, ingest_summary

# By path, not `import conftest`: under a full run that name can resolve to tests/pg/conftest.py.
_spec = importlib.util.spec_from_file_location("tests_conftest",
                                               Path(__file__).resolve().parent / "conftest.py")
_top = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_top)
SUMMARY_HEADERS, SUMMARY_ROWS, _build_reference = (_top.SUMMARY_HEADERS, _top.SUMMARY_ROWS,
                                                   _top._build_reference)

EXTRA = [
    ["E-QUIET", 20, 0, "Normal", "Weekend", None],          # unknown, nothing flagged
    ["E-BLANK", 0, 0, None, None, None],                    # unknown, every cell empty
    ["E-LEAVE", 20, 1, "Annual Leave", "Normal", "Normal"],  # unknown, leave is not a flag
]


@pytest.fixture
def workbook(tmp_path):
    wb = openpyxl.Workbook()
    _build_reference(wb)
    sm = wb.create_sheet("Summary Report")
    sm.append(["Enhanced Attendance Summary Report"])
    sm.append([])
    sm.append(SUMMARY_HEADERS)
    for r in SUMMARY_ROWS + EXTRA:
        sm.append(r)
    path = tmp_path / "d4.xlsx"
    wb.save(path)
    return str(path)


def _parse(path):
    ref = parse_reference(path)
    return ref, ingest_summary(path, ref, year=2026)


def test_unknown_crm_without_a_flag_is_one_undated_exception(workbook):
    _, res = _parse(workbook)
    d4 = [e for e in res.exceptions if e.reason == UNMAPPED_NO_FLAGS]
    assert sorted(e.crm for e in d4) == ["E-BLANK", "E-LEAVE", "E-QUIET"]
    assert all(e.work_date is None for e in d4)        # employee-level: never an attendance day


def test_unknown_crm_with_a_flag_keeps_the_existing_exception_only(workbook):
    _, res = _parse(workbook)
    reasons = {e.reason for e in res.exceptions if e.crm == "E-UNKNOWN"}
    assert reasons == {"unknown_employee"}


def test_no_case_and_no_absence_is_created_for_them(workbook):
    _, res = _parse(workbook)
    quiet = {"E-QUIET", "E-BLANK", "E-LEAVE"}
    assert not [c for c in res.cases if c.employee_crm in quiet]
    cells = [c for c in res.cells if c.crm in quiet]
    assert len(cells) == 3 * 3                          # their cells are stored as they are
    assert not [c for c in cells if c.bucket == "trigger"]
    assert {c.bucket for c in cells if c.crm == "E-BLANK"} == {"blank"}


def test_mapped_employee_without_flags_raises_nothing(workbook):
    _, res = _parse(workbook)
    assert not [e for e in res.exceptions if e.crm == "E-1" and e.reason == UNMAPPED_NO_FLAGS]


def test_upload_preview_counts_and_explains_them(workbook):
    ref, res = _parse(workbook)
    p = ingest_flow.build_preview(res, ref)
    assert p["unmapped_no_flags"] == 3
    line = next(ln for ln in ingest_flow.preview_lines(p) if "not in HC/Structure" in ln)
    assert line.startswith("⚠️ 3 CRM(s)") and "no absence" in line


def test_preview_has_no_d4_line_when_there_are_none(sample_workbook_with_summary):
    ref, res = _parse(sample_workbook_with_summary)
    p = ingest_flow.build_preview(res, ref)
    assert p["unmapped_no_flags"] == 0
    assert not [ln for ln in ingest_flow.preview_lines(p) if "not in HC/Structure" in ln]
