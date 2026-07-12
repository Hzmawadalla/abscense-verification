"""Behavior contract for the 'All Active Employees' single-sheet reference format, the
parse_reference_any dispatcher, and the last-working-day skip in the summary parser (commit 88945b6).
"""
import datetime

from ingestion.reference import parse_active_employees, parse_reference_any
from ingestion.summary import ingest_summary


def _by_crm(items):
    return {i.crm: i for i in items}


def _ref_reasons(ref):
    return {(e.crm, e.reason) for e in ref.exceptions}


# --- parse_active_employees: employee -> line-manager mapping ---

def test_employees_are_mapped_to_their_line_manager(active_employees_workbook):
    ref = parse_active_employees(active_employees_workbook)
    emps = _by_crm(ref.employees)
    assert emps["A-1"].manager_crm == "MGR-1"
    assert emps["A-2"].manager_crm == "MGR-1"


def test_line_manager_becomes_a_verifier_with_email(active_employees_workbook):
    # The verifier is resolved from Line Manager Employee ID -> that manager's own CRM row,
    # and their email is captured for sending.
    ref = parse_active_employees(active_employees_workbook)
    mgrs = _by_crm(ref.managers)
    assert set(mgrs) == {"MGR-1"}
    assert mgrs["MGR-1"].name == "Manager One"
    assert mgrs["MGR-1"].email == "mgr1@x.com"


def test_last_working_day_is_stored_as_exit_date(active_employees_workbook):
    ref = parse_active_employees(active_employees_workbook)
    assert _by_crm(ref.employees)["A-2"].exit_date == datetime.date(2026, 5, 7)


def test_employee_whose_line_manager_is_absent_is_unmapped(active_employees_workbook):
    ref = parse_active_employees(active_employees_workbook)
    assert _by_crm(ref.employees)["A-3"].manager_crm is None
    assert ("A-3", "unmapped_employee") in _ref_reasons(ref)


def test_top_manager_without_own_line_manager_is_unmapped(active_employees_workbook):
    # MGR-1 has no line manager of their own -> flagged as unmapped, even though they verify others.
    ref = parse_active_employees(active_employees_workbook)
    assert _by_crm(ref.employees)["MGR-1"].manager_crm is None
    assert ("MGR-1", "unmapped_employee") in _ref_reasons(ref)


def test_junk_crm_row_is_skipped(active_employees_workbook):
    ref = parse_active_employees(active_employees_workbook)
    assert "N/A" not in _by_crm(ref.employees)
    assert "N/A" not in _by_crm(ref.managers)


def test_blank_line_manager_name_does_not_leak_id_or_email(blank_lm_name_workbook):
    # When the 'Line Manager' name cell is blank, the manager's name must not fall through to the
    # adjacent 'Line Manager Employee ID'/'Line Manager Email' columns. Mapping (by ID) still holds.
    ref = parse_active_employees(blank_lm_name_workbook)
    emps = _by_crm(ref.employees)
    mgrs = _by_crm(ref.managers)
    assert emps["W-1"].manager_crm == "BOSS"
    assert mgrs["BOSS"].name not in {"7000", "boss@x.com"}
    assert mgrs["BOSS"].name is None


# --- parse_reference_any: format dispatch ---

def test_dispatch_selects_active_format(active_employees_workbook):
    # No HC/Structure tabs + 'crm account'/'line manager' on row 2 -> active parser.
    ref = parse_reference_any(active_employees_workbook)
    assert _by_crm(ref.employees)["A-1"].manager_crm == "MGR-1"


def test_dispatch_selects_classic_hc_structure(sample_workbook):
    # HC + Structure tabs present -> classic parser (managers are the distinct TLs).
    ref = parse_reference_any(sample_workbook)
    assert set(_by_crm(ref.managers)) == {"TL-A", "TL-B", "TL-C"}


# --- summary: skip flagged days after an employee's last working day ---

def test_flagged_day_after_last_working_day_is_skipped(active_employees_with_summary_workbook):
    ref = parse_active_employees(active_employees_with_summary_workbook)
    res = ingest_summary(active_employees_with_summary_workbook, ref, year=2026)
    a2_dates = {c.work_date for c in res.cases if c.employee_crm == "A-2"}
    # 06-May before and 07-May on the last working day -> both cased; 08-May after -> skipped.
    assert a2_dates == {datetime.date(2026, 5, 6), datetime.date(2026, 5, 7)}
    assert ("A-2", "after_last_working_day") in {(e.crm, e.reason) for e in res.exceptions}


def test_employee_without_exit_date_is_unaffected(active_employees_with_summary_workbook):
    ref = parse_active_employees(active_employees_with_summary_workbook)
    res = ingest_summary(active_employees_with_summary_workbook, ref, year=2026)
    assert {c.work_date for c in res.cases if c.employee_crm == "A-1"} == {datetime.date(2026, 5, 6)}
    assert ("A-1", "after_last_working_day") not in {(e.crm, e.reason) for e in res.exceptions}
