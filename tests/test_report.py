"""Behavior contract for build_reconciled_report."""
import datetime
import io

import openpyxl

from app.report import CHANGES_HEADER, build_links_workbook, build_reconciled_report, reconcile

LABELS = {"absent": "Absent", "annual_leave": "Annual Leave", "present": "Present"}


def _make_matrix(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary Report"
    ws.append(["CRM", "Normal Days", "15-Jun", "16-Jun"])
    ws.append(["51ahmed", 0, "Absent", "Normal"])
    ws.append(["51sara", 0, "Normal", "Absent"])
    p = tmp_path / "matrix.xlsx"
    wb.save(p)
    return str(p)


def _closed():
    return [{
        "employee_crm": "51AHMED",          # different casing on purpose
        "employee_name": "Ahmed Ali",
        "manager_name": "Zimmy",
        "work_date": datetime.date(2026, 6, 15),
        "source_status": "Absent",
        "final_status": "annual_leave",
        "closed_by": "hrbp",
        "manager_comment": "approved leave",
    }]


def test_overwrites_matched_cell_case_insensitive(tmp_path):
    data = build_reconciled_report(_make_matrix(tmp_path), _closed(), LABELS, year=2026)
    ws = openpyxl.load_workbook(io.BytesIO(data))["Summary Report"]
    # 51ahmed / 15-Jun (row 2, col 3) overwritten despite CRM casing mismatch
    assert ws.cell(row=2, column=3).value == "Annual Leave"


def test_leaves_untouched_cells_unchanged(tmp_path):
    data = build_reconciled_report(_make_matrix(tmp_path), _closed(), LABELS, year=2026)
    ws = openpyxl.load_workbook(io.BytesIO(data))["Summary Report"]
    assert ws.cell(row=2, column=4).value == "Normal"   # ahmed 16-Jun (not a case)
    assert ws.cell(row=3, column=3).value == "Normal"   # sara 15-Jun (different employee)


def test_changes_sheet_records_before_and_after(tmp_path):
    data = build_reconciled_report(_make_matrix(tmp_path), _closed(), LABELS, year=2026)
    rows = list(openpyxl.load_workbook(io.BytesIO(data))["Changes"].iter_rows(values_only=True))
    assert rows[0] == ("CRM", "Employee", "Date", "Before", "After", "TL", "Closed by", "Comment",
                       "In workbook?", "Overwritten?")
    assert rows[1][0] == "51AHMED"
    assert rows[1][2] == "2026-06-15"
    assert rows[1][3] == "Absent"          # before
    assert rows[1][4] == "Annual Leave"    # after
    assert rows[1][8] == "Yes"             # present in this workbook
    assert rows[1][9] == "Yes"             # cell was Absent, so it was overwritten


def test_case_not_in_workbook_is_flagged_no_and_not_written(tmp_path):
    # A closed case inside this file's period for an employee who is not in the uploaded file.
    out_of_file = [{
        "employee_crm": "EGLP-esraamahmoud",
        "employee_name": "Esraa",
        "manager_name": "Zimmy",
        "work_date": datetime.date(2026, 6, 16),  # in the period, but she has no row
        "source_status": "Bereavement Leave",
        "final_status": "present",
        "closed_by": "hrbp",
        "manager_comment": "",
    }]
    data = build_reconciled_report(_make_matrix(tmp_path), out_of_file, LABELS, year=2026)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    # nothing overwritten in the matrix (both existing rows keep their original values)
    ws = wb["Summary Report"]
    assert ws.cell(row=2, column=3).value == "Absent"
    assert ws.cell(row=3, column=3).value == "Normal"
    # she still appears in Changes, flagged "No"
    changes = list(wb["Changes"].iter_rows(values_only=True))
    assert changes[1][0] == "EGLP-esraamahmoud"
    assert changes[1][8] == "No"
    assert changes[1][9] == "No — not in workbook"


def _case(crm, day, final_status="present"):
    return {
        "employee_crm": crm,
        "employee_name": crm,
        "manager_name": "Zimmy",
        "work_date": datetime.date(2026, 6, day),
        "source_status": "Absent",
        "final_status": final_status,
        "closed_by": "hrbp",
        "manager_comment": "",
    }


def _matrix_with(tmp_path, cells):
    """One employee per cell value, all on 15-Jun (column 2)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary Report"
    ws.append(["CRM", "15-Jun"])
    for i, value in enumerate(cells):
        ws.append([f"51emp{i}", value])
    p = tmp_path / "matrix.xlsx"
    wb.save(p)
    return str(p)


def test_overwrites_absent_and_no_show_variants(tmp_path):
    cells = ["Absent", "absent (HD)", "Absent - To be confirmed", "No Show"]
    cases = [_case(f"51emp{i}", 15) for i in range(len(cells))]
    data = build_reconciled_report(_matrix_with(tmp_path, cells), cases, LABELS, year=2026)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    ws = wb["Summary Report"]
    assert [ws.cell(row=r, column=2).value for r in range(2, 2 + len(cells))] == ["Present"] * 4
    assert [row[9] for row in list(wb["Changes"].iter_rows(values_only=True))[1:]] == ["Yes"] * 4


def test_non_absent_cell_is_skipped_not_overwritten(tmp_path):
    cells = ["Annual Leave (Failed)", "Leave Approval Pending", "Normal", None]
    cases = [_case(f"51emp{i}", 15) for i in range(len(cells))]
    data = build_reconciled_report(_matrix_with(tmp_path, cells), cases, LABELS, year=2026)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    ws = wb["Summary Report"]
    assert [ws.cell(row=r, column=2).value for r in range(2, 2 + len(cells))] == cells
    changes = list(wb["Changes"].iter_rows(values_only=True))[1:]
    assert [row[8] for row in changes] == ["Yes"] * 4          # all present in this workbook
    assert [row[9] for row in changes] == ["No — cell is not Absent"] * 4


def test_links_workbook_has_header_and_rows():
    rows = [{"name": "Ahmed", "crm": "TL-A", "email": "a@x.com", "open_cases": 3,
             "link": "https://app/?t=abc"}]
    data = build_links_workbook(rows)
    ws = openpyxl.load_workbook(io.BytesIO(data))["TL links"]
    r = list(ws.iter_rows(values_only=True))
    # changed on purpose (2026-10-16, option A): the column holds the reminder count
    assert r[0] == ("TL name", "CRM", "Email", "Cases to remind", "Link")
    assert r[1] == ("Ahmed", "TL-A", "a@x.com", 3, "https://app/?t=abc")


def test_staff_not_in_attendance_sheet(tmp_path):
    gaps = [{"crm": "51x", "reason": "unmapped_employee", "detail": "no Structure row"}]
    out = build_reconciled_report(_make_matrix(tmp_path), _closed(), LABELS, year=2026, staff_gaps=gaps)
    wb = openpyxl.load_workbook(io.BytesIO(out))
    assert "Staff not in Attendance" in wb.sheetnames
    rows = list(wb["Staff not in Attendance"].iter_rows(values_only=True))
    assert rows[0] == ("CRM", "Reason", "Detail")
    assert rows[1] == ("51x", "unmapped_employee", "no Structure row")


def test_missing_crm_column_raises(tmp_path):
    wb = openpyxl.Workbook()
    wb.active.append(["Employee", "15-Jun"])
    p = tmp_path / "bad.xlsx"
    wb.save(p)
    try:
        build_reconciled_report(str(p), _closed(), LABELS, year=2026)
        assert False, "expected ValueError"
    except ValueError:
        pass


# --- reconcile(): period scoping, counts and file-like input -------------------------------------

def test_cases_outside_the_workbook_period_are_left_out(tmp_path):
    # Closed cases accumulate across months; a June file must not list May or July cases.
    cases = [_case("51ahmed", 15), {**_case("51ahmed", 1), "work_date": datetime.date(2026, 5, 20)},
             {**_case("51sara", 1), "work_date": datetime.date(2026, 7, 2)}]
    result = reconcile(_make_matrix(tmp_path), cases, LABELS, year=2026)
    changes = list(openpyxl.load_workbook(io.BytesIO(result.xlsx))["Changes"].iter_rows(values_only=True))
    assert [row[2] for row in changes[1:]] == ["2026-06-15"]
    assert result.other_period == 2


def test_counts_each_outcome(tmp_path):
    cases = [
        _case("51ahmed", 15),            # Absent cell -> overwritten
        _case("51ahmed", 16),            # Normal cell -> skipped
        _case("51nobody", 15),           # in period, no row -> not in workbook
    ]
    result = reconcile(_make_matrix(tmp_path), cases, LABELS, year=2026)
    assert (result.overwritten, result.skipped_not_absent, result.not_in_workbook,
            result.other_period) == (1, 1, 1, 0)


def test_period_is_the_month_when_dates_share_one(tmp_path):
    assert reconcile(_make_matrix(tmp_path), [], LABELS, year=2026).period == "2026-06"


def test_period_is_a_range_when_dates_span_months(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary Report"
    ws.append(["CRM", "26-Jun", "25-Jul"])
    ws.append(["51ahmed", "Absent", "Normal"])
    p = tmp_path / "cycle.xlsx"
    wb.save(p)
    assert reconcile(str(p), [], LABELS, year=2026).period == "2026-06-26_to_2026-07-25"


def test_accepts_an_in_memory_upload(tmp_path):
    raw = io.BytesIO(open(_make_matrix(tmp_path), "rb").read())   # what Streamlit hands us
    result = reconcile(raw, _closed(), LABELS, year=2026)
    ws = openpyxl.load_workbook(io.BytesIO(result.xlsx))["Summary Report"]
    assert ws.cell(row=2, column=3).value == "Annual Leave"


def test_no_date_columns_raises(tmp_path):
    # A wrong Year or an unexpected header format would otherwise silently match nothing.
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary Report"
    ws.append(["CRM", "Normal Days"])
    p = tmp_path / "nodates.xlsx"
    wb.save(p)
    try:
        reconcile(str(p), _closed(), LABELS, year=2026)
        assert False, "expected ValueError"
    except ValueError:
        pass


# ------------------------------------------------------------------ Phase 2: unresolved = Pending
def _open(crm, day, source="Absent"):
    return {"employee_crm": crm, "employee_name": crm, "manager_name": "TL", "work_date": day,
            "source_status": source, "closed_by": None, "manager_comment": None}


def test_unresolved_case_is_marked_pending_never_left_absent(tmp_path):
    res = reconcile(_make_matrix(tmp_path), [], LABELS, year=2026,
                    unresolved_cases=[_open("51sara", datetime.date(2026, 6, 16))])
    wb = openpyxl.load_workbook(io.BytesIO(res.xlsx))
    assert wb["Summary Report"].cell(row=3, column=4).value == "Pending"
    rows = list(wb["Changes"].iter_rows(values_only=True))
    assert rows[1][4] == "Pending" and rows[1][9] == "Yes — marked Pending (unresolved)"
    assert (res.pending_marked, res.pending_not_marked, res.unresolved) == (1, 0, 1)
    assert res.overwritten == 0                 # Pending is not a final verdict


def test_unresolved_case_on_a_non_absent_cell_keeps_the_cell_and_is_listed(tmp_path):
    res = reconcile(_make_matrix(tmp_path), [], LABELS, year=2026,
                    unresolved_cases=[_open("51sara", datetime.date(2026, 6, 15))])
    wb = openpyxl.load_workbook(io.BytesIO(res.xlsx))
    assert wb["Summary Report"].cell(row=3, column=3).value == "Normal"
    assert list(wb["Changes"].iter_rows(values_only=True))[1][9] == \
        "No — unresolved; cell is not Absent"
    assert (res.pending_marked, res.pending_not_marked) == (0, 1)


def test_unresolved_case_not_in_the_workbook_is_listed_not_dropped(tmp_path):
    res = reconcile(_make_matrix(tmp_path), [], LABELS, year=2026,
                    unresolved_cases=[_open("51ghost", datetime.date(2026, 6, 15))])
    rows = list(openpyxl.load_workbook(io.BytesIO(res.xlsx))["Changes"].iter_rows(values_only=True))
    assert rows[1][0] == "51ghost" and rows[1][8] == "No" and rows[1][9] == "No — not in workbook"
    assert res.unresolved == 1


def test_unresolved_case_from_another_period_is_left_out(tmp_path):
    res = reconcile(_make_matrix(tmp_path), [], LABELS, year=2026,
                    unresolved_cases=[_open("51sara", datetime.date(2026, 7, 16))])
    assert res.unresolved == 0
    assert len(list(openpyxl.load_workbook(io.BytesIO(res.xlsx))["Changes"].iter_rows())) == 1


def test_override_value_is_what_the_export_writes(tmp_path):
    # list_closed_cases carries final_status, which an HRBP override sets: the export follows it
    closed = [{**_closed()[0], "final_status": "present"}]
    data = build_reconciled_report(_make_matrix(tmp_path), closed, LABELS, year=2026)
    assert openpyxl.load_workbook(io.BytesIO(data))["Summary Report"].cell(row=2, column=3).value \
        == "Present"


def test_blank_cells_are_counted_as_missing_and_left_blank(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary Report"
    ws.append(["CRM", "15-Jun", "16-Jun"])
    ws.append(["51ahmed", None, "Absent"])
    ws.append(["51sara", "  ", "Normal"])
    p = tmp_path / "blanks.xlsx"
    wb.save(p)
    res = reconcile(str(p), [], LABELS, year=2026)
    out = openpyxl.load_workbook(io.BytesIO(res.xlsx))["Summary Report"]
    assert res.blank_cells == 2
    assert out.cell(row=2, column=2).value is None      # never turned into Absent


def test_sheet_names_and_matrix_layout_are_unchanged_by_pending(tmp_path):
    src = _make_matrix(tmp_path)
    res = reconcile(src, _closed(), LABELS, year=2026,
                    unresolved_cases=[_open("51sara", datetime.date(2026, 6, 16))])
    out = openpyxl.load_workbook(io.BytesIO(res.xlsx))
    orig = openpyxl.load_workbook(src)["Summary Report"]
    assert out.sheetnames == ["Summary Report", "Changes"]
    m = out["Summary Report"]
    assert (m.max_row, m.max_column) == (orig.max_row, orig.max_column)
    assert [c.value for c in m[1]] == [c.value for c in orig[1]]


def test_old_period_close_row_exports_as_pending_and_keeps_its_closed_by(tmp_path):
    # an hrbp_cutoff case with no decision arrives through unresolved_cases (data.CASE_UNDECIDED):
    # its Absent cell reads Pending, and the Changes row still says where it came from
    cutoff = {**_open("51sara", datetime.date(2026, 6, 16)), "closed_by": "hrbp_cutoff"}
    res = reconcile(_make_matrix(tmp_path), [], LABELS, year=2026, unresolved_cases=[cutoff])
    wb = openpyxl.load_workbook(io.BytesIO(res.xlsx))
    assert wb["Summary Report"].cell(row=3, column=4).value == "Pending"
    row = list(wb["Changes"].iter_rows(values_only=True))[1]
    assert (row[4], row[6]) == ("Pending", "hrbp_cutoff")
    assert (res.unresolved, res.overwritten) == (1, 0)


def test_changes_header_is_unchanged():
    assert CHANGES_HEADER == ["CRM", "Employee", "Date", "Before", "After", "TL", "Closed by",
                              "Comment", "In workbook?", "Overwritten?"]
