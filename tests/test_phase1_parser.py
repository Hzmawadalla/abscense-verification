"""Phase 1 (pure): Attendance Period, Dec -> Jan header dating, every-cell parsing, export dating,
loader statements, TL leave masking."""
import datetime
import io

import openpyxl
import pytest

from app.attendance_view import tl_safe_status
from app.report import reconcile
from ingestion import loader
from ingestion.periods import attendance_period, period_dates
from ingestion.reference import parse_reference
from ingestion.summary import date_columns, ingest_summary

D = datetime.date


# ---------------------------------------------------------------- Attendance Period (15 -> 14)
@pytest.mark.parametrize("day, start, end", [
    (D(2026, 9, 15), D(2026, 9, 15), D(2026, 10, 14)),      # the 15th opens a period
    (D(2026, 10, 14), D(2026, 9, 15), D(2026, 10, 14)),     # the 14th closes it
    (D(2026, 10, 15), D(2026, 10, 15), D(2026, 11, 14)),
    (D(2026, 1, 3), D(2025, 12, 15), D(2026, 1, 14)),       # January -> previous December
    (D(2026, 12, 31), D(2026, 12, 15), D(2027, 1, 14)),     # December -> January
    (D(2027, 1, 14), D(2026, 12, 15), D(2027, 1, 14)),
    (D(2027, 1, 15), D(2027, 1, 15), D(2027, 2, 14)),
    (D(2026, 2, 28), D(2026, 2, 15), D(2026, 3, 14)),       # February
    (D(2028, 2, 29), D(2028, 2, 15), D(2028, 3, 14)),       # leap day
    (D(2026, 3, 1), D(2026, 2, 15), D(2026, 3, 14)),
])
def test_attendance_period_boundaries(day, start, end):
    assert attendance_period(day) == (start, end)


def test_periods_tile_two_full_years_without_gaps():
    day, prev = D(2026, 1, 1), None
    while day <= D(2028, 12, 31):                         # includes 2028-02-29
        start, end = attendance_period(day)
        assert start.day == 15 and end.day == 14 and start <= day <= end
        if prev and prev != (start, end):
            assert start == prev[1] + datetime.timedelta(days=1)   # contiguous, no overlap
        prev = (start, end)
        day += datetime.timedelta(days=1)


def test_period_dates_lists_every_day_and_rejects_non_15th():
    days = period_dates(D(2028, 2, 15))
    assert days[0] == D(2028, 2, 15) and days[-1] == D(2028, 3, 14) and len(days) == 29
    assert len(period_dates(D(2026, 12, 15))) == 31
    with pytest.raises(ValueError):
        period_dates(D(2026, 12, 1))


# ---------------------------------------------------------------- header dating / year rollover
def _dates(headers, year):
    return [d for _, d in date_columns(headers, year)]


def test_dec_to_jan_rolls_into_next_year():
    hdr = ["CRM", "15-Dec", "31-Dec", "01-Jan", "14-Jan"]
    assert _dates(hdr, 2026) == [D(2026, 12, 15), D(2026, 12, 31), D(2027, 1, 1), D(2027, 1, 14)]


@pytest.mark.parametrize("hdr, year, expected", [
    (["15-Jan", "31-Jan", "01-Feb", "14-Feb"], 2027,
     [D(2027, 1, 15), D(2027, 1, 31), D(2027, 2, 1), D(2027, 2, 14)]),
    (["15-Feb", "28-Feb", "01-Mar", "14-Mar"], 2027,
     [D(2027, 2, 15), D(2027, 2, 28), D(2027, 3, 1), D(2027, 3, 14)]),
    (["15-Feb", "29-Feb", "01-Mar"], 2028, [D(2028, 2, 15), D(2028, 2, 29), D(2028, 3, 1)]),
    (["06-May", "07-May", "08-May"], 2026, [D(2026, 5, 6), D(2026, 5, 7), D(2026, 5, 8)]),
])
def test_normal_sheets_keep_todays_dating(hdr, year, expected):
    assert _dates(hdr, year) == expected


def test_feb_29_rollover_lands_in_leap_year():
    # 15-Dec-2027 ... 29-Feb is in 2028 (a leap year) after the rollover
    assert _dates(["15-Dec", "01-Jan", "29-Feb"], 2027) == [D(2027, 12, 15), D(2028, 1, 1),
                                                           D(2028, 2, 29)]


def test_only_a_year_boundary_rolls_over_and_dated_headers_never_shift():
    assert _dates(["06-May", "05-Apr"], 2026) == [D(2026, 5, 6), D(2026, 4, 5)]   # out of order
    real = datetime.datetime(2026, 12, 20)
    assert _dates([real, "01-Jan"], 2026) == [D(2026, 12, 20), D(2026, 1, 1)]


# ---------------------------------------------------------------- every cell, first row wins
def _sheet(tmp_path, headers, rows, name="s.xlsx"):
    wb = openpyxl.load_workbook(_ref_path(tmp_path))
    ws = wb["Summary Report"]
    ws.delete_rows(1, ws.max_row)
    ws.append(["Title"])
    ws.append([])
    ws.append(headers)
    for r in rows:
        ws.append(r)
    p = tmp_path / name
    wb.save(p)
    return str(p)


_REF = {}


def _ref_path(tmp_path):
    return _REF["path"]


@pytest.fixture(autouse=True)
def _remember_ref(sample_workbook_with_summary):
    _REF["path"] = sample_workbook_with_summary


def test_every_cell_is_represented_with_its_bucket(tmp_path, sample_workbook_with_summary):
    ref = parse_reference(sample_workbook_with_summary)
    path = _sheet(tmp_path, ["CRM", "Normal Days", "06-May", "07-May", "08-May", "09-May", "10-May"], [
        ["E-1", 3, "Normal", "Sick Leave (HD)", None, "Weird XYZ", "Not Yet Hired"],
        ["E-UNKNOWN", 1, "Absent", "Normal", "Normal", "Normal", "Normal"],
    ])
    res = ingest_summary(path, ref, year=2026)
    cells = {(c.crm_key, c.work_date.day): c for c in res.cells}
    assert len(res.cells) == 10                                 # 2 rows x 5 dates, blanks included
    assert cells[("e-1", 6)].bucket == "skip" and cells[("e-1", 6)].canonical_status == "Normal"
    assert cells[("e-1", 7)].is_half_day and cells[("e-1", 7)].canonical_status == "Sick Leave"
    blank = cells[("e-1", 8)]
    assert blank.bucket == "blank" and blank.raw_value is None
    assert cells[("e-1", 9)].bucket == "unknown" and cells[("e-1", 9)].raw_value == "Weird XYZ"
    assert cells[("e-1", 10)].canonical_status == "Not Yet Hired"
    assert cells[("e-1", 6)].manager_crm == "TL-A"
    unmapped = cells[("e-unknown", 6)]                          # kept, with no TL
    assert unmapped.bucket == "trigger" and unmapped.manager_crm is None
    assert res.stats["date_range"] == (D(2026, 5, 6), D(2026, 5, 10))


def test_duplicate_row_first_wins_logged_and_cases_unchanged(tmp_path, sample_workbook_with_summary):
    ref = parse_reference(sample_workbook_with_summary)
    path = _sheet(tmp_path, ["CRM", "06-May"], [["E-1", "Normal"], ["e-1", "Absent"]])
    res = ingest_summary(path, ref, year=2026)
    assert [(c.raw_value, c.source_row) for c in res.cells] == [("Normal", 4)]   # first row
    dup = [e for e in res.exceptions if e.reason == "duplicate_row"]
    assert len(dup) == 1 and "first at row 4" in dup[0].raw_value
    # case creation is exactly as before: the duplicate row's Absent still creates its case
    assert [(c.employee_crm, c.source_status) for c in res.cases] == [("E-1", "Absent")]


def test_existing_case_output_is_unchanged_by_cell_storage(sample_workbook_with_summary):
    ref = parse_reference(sample_workbook_with_summary)
    res = ingest_summary(sample_workbook_with_summary, ref, year=2026)
    assert sorted((c.employee_crm, c.work_date.day, c.source_status) for c in res.cases) == [
        ("E-1", 6, "Absent"), ("E-2", 8, "Unpaid Leave (HD) - To Be Confirmed")]
    assert len(res.cells) == 4 * 3


def test_cross_year_sheet_dates_cases_and_cells_in_the_next_year(tmp_path, sample_workbook_with_summary):
    ref = parse_reference(sample_workbook_with_summary)
    path = _sheet(tmp_path, ["CRM", "31-Dec", "01-Jan"], [["E-1", "Normal", "Absent"]])
    res = ingest_summary(path, ref, year=2026)
    assert [c.work_date for c in res.cases] == [D(2027, 1, 1)]
    assert sorted(c.work_date for c in res.cells) == [D(2026, 12, 31), D(2027, 1, 1)]


def test_export_dates_a_cross_year_sheet_like_ingestion(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary Report"
    ws.append(["CRM", "31-Dec", "01-Jan"])
    ws.append(["51ahmed", "Normal", "Absent"])
    buf = io.BytesIO()
    wb.save(buf)
    closed = [{"employee_crm": "51ahmed", "employee_name": "A", "manager_name": "Z",
               "work_date": D(2027, 1, 1), "source_status": "Absent", "final_status": "present",
               "closed_by": "tl", "manager_comment": ""}]
    result = reconcile(io.BytesIO(buf.getvalue()), closed, {"present": "Present"}, year=2026)
    assert result.overwritten == 1 and result.other_period == 0


# ---------------------------------------------------------------- loader
class FakeDB:
    def __init__(self):
        self.calls = []

    def one(self, sql, params):
        self.calls.append(("one", sql, params))
        return ("run-1",)

    def many(self, sql, rows):
        self.calls.append(("many", sql, list(rows)))


def test_loader_records_range_hash_full_cells_and_stores_cells_last(sample_workbook_with_summary):
    ref = parse_reference(sample_workbook_with_summary)
    res = ingest_summary(sample_workbook_with_summary, ref, year=2026)
    db = FakeDB()
    loader.load_ingestion(db, res, source_filename="wb.xlsx", file_sha256="abc")
    run_params = db.calls[0][2]
    assert run_params[1:3] == (D(2026, 5, 6), D(2026, 5, 8))          # range from the sheet
    assert run_params[-2:] == ("abc", True)                            # hash, full_cells
    assert [c[1] for c in db.calls[1:]] == [loader.AUDIT_SOURCE_CHANGE, loader.UPSERT_CASE,
                                            loader.INSERT_EXCEPTION, loader.INSERT_DAY]
    day_rows = db.calls[4][2]
    assert len(day_rows) == len(res.cells) and all(r[0] == "run-1" for r in day_rows)


# ---------------------------------------------------------------- TL leave masking
@pytest.mark.parametrize("value, shown", [
    ("Sick Leave", "Leave"), ("Bereavement Leave", "Leave"), ("Marriage Leave", "Leave"),
    ("Paternity Leave", "Leave"), ("Annual Leave", "Leave"), ("Leave Approval Pending", "Leave"),
    ("sick_leave", "leave"), ("annual_leave", "leave"),
    ("Normal", "Normal"), ("Absent", "Absent"), ("No Show", "No Show"), ("Late", "Late"),
    ("Half Day", "Half Day"), ("Not Yet Hired", "Not Yet Hired"), ("No Leave", "No Leave"),
    ("present", "present"), (None, None),
])
def test_tl_safe_status(value, shown):
    assert tl_safe_status(value) == shown
