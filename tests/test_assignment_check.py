"""Phase 3 decision F: advisory warnings for suspicious team changes in a reference upload, and the
parsers' duplicate / conflicting-CRM reporting. Nothing here may change what an upload loads."""
import openpyxl

from app import assignment_check as ac
from app.ingest_flow import preview_lines
from ingestion.reference import parse_active_employees, parse_reference


def _changes(incoming, current, history=None):
    return ac.classify(incoming, current, history or {})


# ------------------------------------------------------------------ classification (pure)
def test_a_new_employee_is_not_a_move():
    c = _changes({"E-NEW": "TL-A"}, {})
    assert c.moves == () and c.listed_existing == 0


def test_the_same_tl_again_is_not_a_move():
    c = _changes({"E-1": "TL-A"}, {"E-1": "TL-A"}, {"E-1": ["TL-A"]})
    assert c.moves == () and c.reverse == () and ac.warning_lines(c, {}) == []


def test_a_first_move_is_a_move_but_not_a_reverse():
    c = _changes({"E-1": "TL-B"}, {"E-1": "TL-A"}, {"E-1": ["TL-A"]})
    assert c.moves == ("E-1",) and c.reverse == ()


def test_moving_back_to_an_earlier_recorded_tl_is_a_reverse_move():
    # history A then B, current B; an OLDER file still showing A
    c = _changes({"E-1": "TL-A"}, {"E-1": "TL-B"}, {"E-1": ["TL-A", "TL-B"]})
    assert c.reverse == ("E-1",)
    line = next(ln for ln in ac.warning_lines(c, {}) if "back" in ln)
    assert "older" in line and "today's date" in line and "E-1" in line


def test_a_move_recorded_only_by_old_code_still_counts_as_reverse():
    # history A (seed), employees.manager_id changed to B without a stretch, file shows A again
    assert _changes({"E-1": "TL-A"}, {"E-1": "TL-B"}, {"E-1": ["TL-A"]}).reverse == ("E-1",)


def test_losing_the_tl_is_reported_and_explains_the_permanent_gap():
    c = _changes({"E-1": None}, {"E-1": "TL-A"}, {"E-1": ["TL-A"]})
    assert c.to_none == ("E-1",) and c.reverse == ()
    assert any("no TL" in ln and "no TL's team view" in ln for ln in ac.warning_lines(c, {}))


def test_broad_change_needs_both_the_minimum_and_the_share():
    current = {f"E-{i}": "TL-A" for i in range(200)}
    nine = {f"E-{i}": "TL-B" for i in range(9)}
    assert not _changes(nine, current).broad                       # below BROAD_MIN
    ten_of_200 = {**{f"E-{i}": "TL-A" for i in range(200)}, **{f"E-{i}": "TL-B" for i in range(10)}}
    assert not _changes(ten_of_200, current).broad                 # 5% < BROAD_SHARE
    twenty_of_200 = {**{f"E-{i}": "TL-A" for i in range(200)}, **{f"E-{i}": "TL-B" for i in range(20)}}
    c = _changes(twenty_of_200, current)
    assert c.broad and any("20 of 200" in ln and "10%" in ln for ln in ac.warning_lines(c, {}))


def test_warnings_name_at_most_a_sample_of_crms():
    c = _changes({f"E-{i}": None for i in range(8)}, {f"E-{i}": "TL-A" for i in range(8)})
    line = next(ln for ln in ac.warning_lines(c, {}) if "no TL" in ln)
    assert f"and {8 - ac.SAMPLE} more" in line


def test_the_preview_shows_assignment_warnings_after_the_existing_ones():
    c = _changes({"E-1": "TL-A"}, {"E-1": "TL-B"}, {"E-1": ["TL-A", "TL-B"]})
    p = {"first_date": __import__("datetime").date(2026, 9, 15),
         "last_date": __import__("datetime").date(2026, 9, 20), "days": 6, "periods": [],
         "cells": 0, "cases": 0, "employees_in_sheet": 0, "exceptions": 0, "mapped": (None, None),
         "duplicate_dates": [], "duplicate_rows": 0, "earlier_uploads": 0,
         "assignment_warnings": ac.warning_lines(c, {})}
    assert any("back" in ln for ln in preview_lines(p))


def test_no_assignment_data_means_no_assignment_warning():
    assert ac.warning_lines(None, {}) == [] and ac.warning_lines(None, None) == []


# ------------------------------------------------------------------ parsers: duplicates (report only)
def _hc_structure(tmp_path, hc_rows, st_rows):
    wb = openpyxl.Workbook()
    hc = wb.active
    hc.title = "HC"
    hc.append(["CRM", "Full Name", "Employee Status", "Work Email address"])
    for r in hc_rows:
        hc.append(r)
    st = wb.create_sheet("Structure")
    st.append(["SM/LTL/STL", "TL/Coach Lead", "Team", "CRM"])
    for r in st_rows:
        st.append(r)
    path = tmp_path / "ref.xlsx"
    wb.save(path)
    return str(path)


def test_one_crm_under_two_tls_in_structure_is_reported_and_the_last_row_still_wins(tmp_path):
    path = _hc_structure(tmp_path,
                         [["TL-A", "A", "Active", "a@x"], ["TL-B", "B", "Active", "b@x"],
                          ["E-1", "One", "Active", "e1@x"], ["E-2", "Two", "Active", "e2@x"]],
                         [["SM", "TL-A", "T", "E-1"], ["SM", "TL-B", "T", "E-1"],   # conflict
                          ["SM", "TL-A", "T", "E-2"], ["SM", "TL-A", "T", "E-2"]])  # repeat, same TL
    ref = parse_reference(path)
    assert {e.crm: e.manager_crm for e in ref.employees}["E-1"] == "TL-B"    # unchanged: last row
    assert ref.stats["conflicting_tl_crms"] == ["E-1"]
    assert ref.stats["structure_duplicate_crms"] == ["E-1", "E-2"]
    line = next(ln for ln in ac.warning_lines(None, ref.stats) if "different TLs" in ln)
    assert "last Structure row" in line and "E-1" in line


def test_a_crm_repeated_in_hc_is_reported_and_the_first_row_still_wins(tmp_path):
    path = _hc_structure(tmp_path,
                         [["TL-A", "A", "Active", "a@x"], ["E-1", "First", "Active", "e1@x"],
                          ["e-1", "Second", "Active", "e1b@x"]],
                         [["SM", "TL-A", "T", "E-1"]])
    ref = parse_reference(path)
    assert {e.crm: e.name for e in ref.employees}["E-1"] == "First"          # unchanged
    assert ref.stats["duplicate_crms"] == ["E-1"]
    assert any("first HC row" in ln for ln in ac.warning_lines(None, ref.stats))


def test_a_clean_reference_reports_no_duplicates(sample_workbook):
    ref = parse_reference(sample_workbook)
    assert ref.stats["duplicate_crms"] == [] and ref.stats["conflicting_tl_crms"] == []
    assert ac.warning_lines(None, ref.stats) == []


def test_line_manager_export_repeated_crm_with_another_manager_is_reported(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "All Active Employees"
    ws.append(["c"] * 10)
    ws.append(["CRM account", "Employee ID", "Name", "Email", "Department", "Employee Status",
               "Last Working Day", "Line Manager", "Line Manager Employee ID", "Line Manager Email"])
    ws.append(["A-1", "1", "Alpha", "a1@x", "EA", "Active", None, "M One", "91", "m1@x"])
    ws.append(["A-1", "1", "Alpha", "a1@x", "EA", "Active", None, "M Two", "92", "m2@x"])
    path = tmp_path / "active.xlsx"
    wb.save(path)
    ref = parse_active_employees(str(path))
    assert [e.manager_crm for e in ref.employees] == ["m1@x"]                 # unchanged: first row
    assert ref.stats["duplicate_crms"] == ["A-1"] and ref.stats["conflicting_tl_crms"] == ["A-1"]


def test_a_failing_read_never_blocks_the_preview_and_says_the_check_did_not_run():
    class BrokenConn:
        rolled_back = False

        def cursor(self):
            raise RuntimeError("connection lost")

        def rollback(self):
            BrokenConn.rolled_back = True
    ref = type("Ref", (), {"employees": [type("E", (), {"crm": "E-1", "manager_crm": "TL-A"})()],
                           "stats": {}})()
    result = ac.read_changes_or_none(BrokenConn(), ref)
    assert result == ac.UNAVAILABLE and BrokenConn.rolled_back
    lines = ac.warning_lines(result, {})
    assert len(lines) == 1 and "could not run" in lines[0]
