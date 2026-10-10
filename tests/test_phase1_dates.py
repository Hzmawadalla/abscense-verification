"""Phase 1 date safety (M1 / M2 / L1 / L3), pure: repeated date columns, date order, the year
meaning 'year of the FIRST date column', duplicate employee rows. Synthetic workbooks only."""
import datetime

import openpyxl
import pytest

from ingestion.periods import attendance_period
from ingestion.reference import parse_reference
from ingestion.summary import UnsafeWorkbookError, date_columns, ingest_summary

D = datetime.date


@pytest.fixture()
def ref(sample_workbook_with_summary):
    return parse_reference(sample_workbook_with_summary)


@pytest.fixture()
def sheet(tmp_path, sample_workbook_with_summary):
    def make(headers, rows, name="s.xlsx"):
        wb = openpyxl.load_workbook(sample_workbook_with_summary)
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
    return make


def _codes(res, reason):
    return [e for e in res.exceptions if e.reason == reason]


# ------------------------------------------------------------------ M1 repeated date columns
def test_identical_repeated_header_keeps_first_column_and_records_it(sheet, ref):
    p = sheet(["CRM", "06-May", "07-May", "07-May"],
              [["E-1", "Normal", "Absent", "absent "], ["E-2", "Normal", None, None]])
    res = ingest_summary(p, ref, year=2026)
    assert sorted(c.work_date.day for c in res.cells if c.crm_key == "e-1") == [6, 7]
    assert [(c.work_date, c.source_status) for c in res.cases] == [(D(2026, 5, 7), "Absent")]
    dup = _codes(res, "duplicate_date_column")
    assert len(dup) == 1 and dup[0].work_date == D(2026, 5, 7) and dup[0].crm is None
    assert "columns C, D" in dup[0].raw_value
    assert res.stats["duplicate_dates"] == [D(2026, 5, 7)]


def test_same_date_in_two_header_formats_is_a_repeat(sheet, ref):
    p = sheet(["CRM", "07-May", datetime.datetime(2026, 5, 7)], [["E-1", "Normal", "Normal"]])
    res = ingest_summary(p, ref, year=2026)
    assert len(res.cells) == 1 and len(_codes(res, "duplicate_date_column")) == 1


def test_repeat_that_only_appears_after_year_resolution(sheet, ref):
    p = sheet(["CRM", "31-Dec", "01-Jan", "1-Jan-2027"], [["E-1", "Normal", "Normal", "Normal"]])
    res = ingest_summary(p, ref, year=2026)
    assert sorted(c.work_date for c in res.cells) == [D(2026, 12, 31), D(2027, 1, 1)]
    assert _codes(res, "duplicate_date_column")[0].work_date == D(2027, 1, 1)


@pytest.mark.parametrize("a, b", [("Absent", "Normal"), ("Absent", None), (None, "Late")])
def test_conflicting_repeated_dates_reject_the_whole_upload(sheet, ref, a, b):
    p = sheet(["CRM", "06-May", "07-May", "07-May"],
              [["E-1", "Normal", "Normal", "Normal"], ["E-2", "Normal", a, b]])
    with pytest.raises(UnsafeWorkbookError) as e:
        ingest_summary(p, ref, year=2026)
    msg = str(e.value)
    assert "07-May-2026 in columns C, D" in msg and "Nothing was loaded" in msg
    assert "Absent" not in msg and "Late" not in msg               # no cell contents echoed


def test_junk_rows_do_not_create_a_conflict(sheet, ref):
    p = sheet(["CRM", "07-May", "07-May"], [["E-1", "Normal", "Normal"], ["N/A", "x", "y"],
                                            [None, "a", "b"]])
    res = ingest_summary(p, ref, year=2026)
    assert len(res.cells) == 1


def test_mixed_date_and_text_headers(sheet, ref):
    p = sheet(["CRM", "Normal Days", "06-May", "Total", "07-May"], [["E-1", 3, "Normal", 9, "Late"]])
    res = ingest_summary(p, ref, year=2026)
    assert sorted(c.work_date.day for c in res.cells) == [6, 7]
    assert _codes(res, "duplicate_date_column") == []


# ------------------------------------------------------------------ L1 duplicate employee rows
def test_duplicate_employee_row_is_logged_and_never_creates_a_case(sheet, ref):
    p = sheet(["CRM", "06-May", "07-May"], [["E-1", "Normal", "Normal"], ["e-1", "Absent", "No Show"]])
    res = ingest_summary(p, ref, year=2026)
    assert res.cases == []
    assert [c.raw_value for c in res.cells] == ["Normal", "Normal"]
    assert len(_codes(res, "duplicate_row")) == 1


def test_duplicate_row_and_duplicate_date_together(sheet, ref):
    # The repeated ROW is ignored for cells and cases, but it still holds conflicting values for
    # the repeated DATE: ambiguous source data is rejected rather than half-trusted.
    p = sheet(["CRM", "07-May", "07-May"], [["E-1", "Absent", "Absent"], ["E-1", "Normal", "Late"]])
    with pytest.raises(UnsafeWorkbookError):
        ingest_summary(p, ref, year=2026)


# ------------------------------------------------------------------ M2 / L3 order and the year
@pytest.mark.parametrize("headers, year, first, last", [
    (["15-Dec", "31-Dec", "01-Jan", "14-Jan"], 2026, D(2026, 12, 15), D(2027, 1, 14)),
    (["15-Jan", "31-Jan", "01-Feb", "14-Feb"], 2026, D(2026, 1, 15), D(2026, 2, 14)),
    (["15-Feb", "28-Feb", "01-Mar"], 2026, D(2026, 2, 15), D(2026, 3, 1)),     # normal February
    (["15-Feb", "29-Feb", "01-Mar"], 2028, D(2028, 2, 15), D(2028, 3, 1)),     # leap February
    (["06-May-2026", "07-May-2026"], 2030, D(2026, 5, 6), D(2026, 5, 7)),      # 4-digit headers
])
def test_resolved_range(sheet, ref, headers, year, first, last):
    p = sheet(["CRM", *headers], [["E-1", *(["Normal"] * len(headers))]])
    assert ingest_summary(p, ref, year=year).stats["date_range"] == (first, last)


def test_29_feb_in_a_normal_year_is_not_a_date(sheet, ref):
    p = sheet(["CRM", "28-Feb", "29-Feb", "01-Mar"], [["E-1", "Normal", "Normal", "Normal"]])
    res = ingest_summary(p, ref, year=2026)
    assert [c.work_date for c in res.cells] == [D(2026, 2, 28), D(2026, 3, 1)]


def test_real_date_then_yearless_header_is_rejected_not_misdated(sheet, ref):
    """L3, confirmed: a real 31-Dec-2026 cell followed by '01-Jan' is dated 2026-01-01 (no
    rollover). The order check turns that silent misdating into a clear rejection."""
    hdr = [datetime.datetime(2026, 12, 31), "01-Jan"]
    assert [d for _, d in date_columns(hdr, 2026)] == [D(2026, 12, 31), D(2026, 1, 1)]   # defect
    p = sheet(["CRM", *hdr], [["E-1", "Normal", "Normal"]])
    with pytest.raises(UnsafeWorkbookError, match="not in order"):
        ingest_summary(p, ref, year=2026)


def test_out_of_order_columns_are_rejected(sheet, ref):
    p = sheet(["CRM", "07-May", "06-May"], [["E-1", "Normal", "Normal"]])
    with pytest.raises(UnsafeWorkbookError, match="Year"):
        ingest_summary(p, ref, year=2026)


def test_a_wrong_year_is_visible_in_the_range(sheet, ref):
    """The old habit (entering the LAST column's year) shifts the sheet by a year. The sheet alone
    cannot reveal it, so the preview shows the resolved range before anything loads."""
    p = sheet(["CRM", "15-Dec", "01-Jan"], [["E-1", "Normal", "Normal"]])
    assert ingest_summary(p, ref, year=2027).stats["date_range"] == (D(2027, 12, 15), D(2028, 1, 1))


@pytest.mark.parametrize("headers, periods", [
    (["13-May", "14-May", "15-May", "16-May"], 2),                                  # one boundary
    (["10-Apr", "20-Apr", "10-May", "20-May", "10-Jun", "20-Jun"], 4),              # many periods
])
def test_uploads_spanning_attendance_periods_are_accepted(sheet, ref, headers, periods):
    p = sheet(["CRM", *headers], [["E-1", *(["Normal"] * len(headers))]])
    res = ingest_summary(p, ref, year=2026)
    assert len({attendance_period(c.work_date) for c in res.cells}) == periods


def test_sheet_longer_than_two_months_and_62_days_is_accepted(sheet, ref):
    """No length limit: 15-Oct-2026 -> 14-Jan-2027 is 92 days over three attendance periods."""
    hdr = ["15-Oct", "31-Oct", "15-Nov", "30-Nov", "15-Dec", "31-Dec", "01-Jan", "14-Jan"]
    res = ingest_summary(sheet(["CRM", *hdr], [["E-1", *(["Normal"] * len(hdr))]]), ref, year=2026)
    first, last = res.stats["date_range"]
    assert (first, last) == (D(2026, 10, 15), D(2027, 1, 14)) and (last - first).days + 1 == 92
    assert sorted({attendance_period(c.work_date) for c in res.cells}) == [
        (D(2026, 10, 15), D(2026, 11, 14)), (D(2026, 11, 15), D(2026, 12, 14)),
        (D(2026, 12, 15), D(2027, 1, 14))]


def test_a_full_year_of_columns_rolls_over_once(sheet, ref):
    hdr = [f"15-{m}" for m in ("Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Jan", "Feb", "Mar",
                               "Apr", "May", "Jun")]
    res = ingest_summary(sheet(["CRM", *hdr], [["E-1", *(["Normal"] * 12)]]), ref, year=2026)
    assert res.stats["date_range"] == (D(2026, 7, 15), D(2027, 6, 15))
    assert len({attendance_period(c.work_date) for c in res.cells}) == 12


@pytest.mark.parametrize("hdr, jan_like", [
    (["30-Oct", "05-Jan"], D(2027, 1, 5)),          # Nov/Dec columns missing: 3 months forward
    (["20-Aug", "10-Jan"], D(2027, 1, 10)),         # 5 months forward: still a rollover
])
def test_a_gap_across_the_year_end_rolls_over(sheet, ref, hdr, jan_like):
    res = ingest_summary(sheet(["CRM", *hdr], [["E-1", "Normal", "Normal"]]), ref, year=2026)
    assert res.stats["date_range"][1] == jan_like
    assert attendance_period(jan_like)[0] == D(2026, 12, 15)


@pytest.mark.parametrize("hdr", [
    ["06-May", "05-Apr"],        # out of order by a month (11 months 'forward'): never a rollover
    ["15-Jul", "15-Jan"],        # 6 months: rollover or out of order? ambiguous -> rejected
    ["10-Sep", "09-Sep"],        # same month, a day back
])
def test_ambiguous_or_out_of_order_sequences_are_rejected(sheet, ref, hdr):
    with pytest.raises(UnsafeWorkbookError, match="not in order"):
        ingest_summary(sheet(["CRM", *hdr], [["E-1", "Normal", "Normal"]]), ref, year=2026)


def test_repeated_crm_rows_count_toward_a_date_conflict(sheet, ref):
    """Reviewer M1, kept on purpose: a repeated date column must agree on EVERY data row, including
    a repeated CRM row that first-row-wins later ignores. Ambiguous source data is rejected rather
    than partly trusted (fail safe; nothing is written)."""
    p = sheet(["CRM", "07-May", "07-May"], [["E-1", "Normal", "Normal"], ["E-1", "Absent", None]])
    with pytest.raises(UnsafeWorkbookError):
        ingest_summary(p, ref, year=2026)
