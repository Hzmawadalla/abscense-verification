"""Behavior contract for ingest_summary (SPEC §6.2)."""
import datetime

from ingestion.reference import parse_reference
from ingestion.summary import ingest_summary, parse_day_header

YEAR = 2026


def _ingest(path):
    ref = parse_reference(path)
    return ingest_summary(path, ref, year=YEAR)


def _cases_for(res, crm):
    return [c for c in res.cases if c.employee_crm == crm]


def _reasons(res):
    return {(e.crm, e.reason) for e in res.exceptions}


def test_absent_day_creates_case_for_mapped_employee(sample_workbook_with_summary):
    res = _ingest(sample_workbook_with_summary)
    cases = _cases_for(res, "E-1")
    assert len(cases) == 1
    c = cases[0]
    assert c.work_date == datetime.date(2026, 5, 6)
    assert c.manager_crm == "TL-A"
    assert c.source_status == "Absent"


def test_clean_and_approved_leave_days_create_no_case(sample_workbook_with_summary):
    res = _ingest(sample_workbook_with_summary)
    # E-1's Normal (07) and Annual Leave (08) must not produce cases.
    assert {c.work_date for c in _cases_for(res, "E-1")} == {datetime.date(2026, 5, 6)}


def test_failed_leave_and_annotation_trigger_cases(sample_workbook_with_summary):
    res = _ingest(sample_workbook_with_summary)
    dates = {c.work_date for c in _cases_for(res, "E-2")}
    assert dates == {datetime.date(2026, 5, 7), datetime.date(2026, 5, 8)}


def test_half_day_flag_parsed_from_annotation(sample_workbook_with_summary):
    res = _ingest(sample_workbook_with_summary)
    hd = next(c for c in _cases_for(res, "E-2") if c.work_date == datetime.date(2026, 5, 8))
    assert hd.is_half_day is True


def test_unmapped_employee_is_blocked_not_cased(sample_workbook_with_summary):
    res = _ingest(sample_workbook_with_summary)
    assert _cases_for(res, "E-4") == []
    assert ("E-4", "blocked_unmapped") in _reasons(res)


def test_employee_absent_from_hc_is_flagged(sample_workbook_with_summary):
    res = _ingest(sample_workbook_with_summary)
    assert ("E-UNKNOWN", "unknown_employee") in _reasons(res)


def test_unknown_status_is_flagged_not_dropped(sample_workbook_with_summary):
    res = _ingest(sample_workbook_with_summary)
    assert ("E-2", "unknown_status") in _reasons(res)


def test_day_header_parsing():
    assert parse_day_header("06-May", 2026) == datetime.date(2026, 5, 6)
    assert parse_day_header(datetime.datetime(2026, 6, 14), 2026) == datetime.date(2026, 6, 14)
    assert parse_day_header("Normal Days", 2026) is None
