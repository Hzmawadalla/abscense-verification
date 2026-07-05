"""Behavior contract for parse_reference (SPEC §6.1)."""
import datetime

from ingestion.reference import parse_reference


def _by_crm(items):
    return {i.crm: i for i in items}


def test_managers_are_distinct_tls_enriched_from_hc(sample_workbook):
    ref = parse_reference(sample_workbook)
    managers = _by_crm(ref.managers)
    assert set(managers) == {"TL-A", "TL-B", "TL-C"}
    assert managers["TL-A"].name == "Alice TL"
    assert managers["TL-A"].email == "alice@x.com"


def test_tl_missing_from_hc_has_no_email_and_is_flagged(sample_workbook):
    ref = parse_reference(sample_workbook)
    assert _by_crm(ref.managers)["TL-B"].email is None
    assert ("TL-B", "manager_not_in_hc") in {(e.crm, e.reason) for e in ref.exceptions}


def test_departed_employees_are_excluded(sample_workbook):
    ref = parse_reference(sample_workbook)
    crms = _by_crm(ref.employees)
    assert "E-3" not in crms
    assert set(crms) == {"TL-A", "E-1", "E-2", "E-4", "E-5", "TL-C", "E-6"}


def test_employees_get_their_team_leader(sample_workbook):
    ref = parse_reference(sample_workbook)
    emps = _by_crm(ref.employees)
    assert emps["E-1"].manager_crm == "TL-A"
    assert emps["E-2"].manager_crm == "TL-A"
    assert emps["E-5"].manager_crm == "TL-B"
    assert emps["E-5"].sm_crm == "SM-B"


def test_employee_without_structure_row_is_unmapped(sample_workbook):
    ref = parse_reference(sample_workbook)
    assert _by_crm(ref.employees)["E-4"].manager_crm is None
    assert ("E-4", "unmapped_employee") in {(e.crm, e.reason) for e in ref.exceptions}


def test_structure_only_person_flagged_not_in_hc(sample_workbook):
    ref = parse_reference(sample_workbook)
    assert ("E-BC", "employee_not_in_hc") in {(e.crm, e.reason) for e in ref.exceptions}


def test_junk_crm_is_excluded_and_flagged(sample_workbook):
    ref = parse_reference(sample_workbook)
    assert "N/A" not in _by_crm(ref.employees)
    assert ("N/A", "invalid_crm") in {(e.crm, e.reason) for e in ref.exceptions}


def test_crm_matching_is_case_insensitive(sample_workbook):
    # Structure refers to TL as 'tl-c'; HC stores 'TL-C'. Must resolve, using HC's casing.
    ref = parse_reference(sample_workbook)
    assert _by_crm(ref.employees)["E-6"].manager_crm == "TL-C"
    mgr = _by_crm(ref.managers)["TL-C"]
    assert mgr.email == "carol@x.com"
    assert "TL-C" not in {e.crm for e in ref.exceptions}


def test_nickname_tl_resolved_via_alias(sample_workbook):
    # TL-B is not in HC; an alias supplies its email so it needn't be flagged.
    ref = parse_reference(sample_workbook, aliases={"tl-b": {"email": "bob@x.com", "name": "Bob TL"}})
    mgr = _by_crm(ref.managers)["TL-B"]
    assert mgr.email == "bob@x.com"
    assert ("TL-B", "manager_not_in_hc") not in {(e.crm, e.reason) for e in ref.exceptions}


def test_dates_are_parsed(sample_workbook):
    ref = parse_reference(sample_workbook)
    assert _by_crm(ref.employees)["E-1"].join_date == datetime.date(2024, 2, 1)
