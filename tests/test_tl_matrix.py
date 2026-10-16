"""app.tl_matrix: shaping TL-safe rows into the team matrix, summary and filters. Pure."""
import datetime as dt

from app import tl_matrix as m
from app.attendance_view import TL_ROW_FIELDS

D = dt.date
P = D(2026, 9, 15)


def row(crm, day, eff, coverage="full", visibility="team", case_id=None, employment=None,
        reviewable=False, name=None):
    r = {k: None for k in TL_ROW_FIELDS}
    r.update(employee_id=crm, employee_crm=crm, employee_name=name or crm, work_date=day,
             tl_effective=eff, coverage=coverage, visibility=visibility, case_id=case_id,
             employment=employment, reviewable=reviewable)
    return r


def test_cells_never_turn_missing_or_blank_into_absent():
    assert m.cell(row("E", P, None, coverage="none")) == m.MISSING
    assert m.cell(row("E", P, "Missing", coverage="none")) == "Missing"
    assert m.cell(row("E", P, "Blank")) == "Blank"
    assert m.cell(row("E", P, "Absent")) == "Absent"


def test_withheld_and_not_employed_cells():
    assert m.cell(row("E", P, None, coverage="none", visibility="case_only")) == m.WITHHELD
    assert m.cell(row("E", P, "Pending", visibility="case_only", case_id="c1")) == "Pending"
    assert m.cell(row("E", P, "Missing", coverage="none", employment="before_join")) == m.NOT_EMPLOYED
    assert m.cell(row("E", P, "Absent", employment="after_exit")) == "Absent"   # a real cell shows


def test_summary_counts_and_denominator():
    rows = [row("E1", P, "Present"), row("E1", P + dt.timedelta(1), "Blank"),
            row("E1", P + dt.timedelta(2), "Pending", coverage="flagged_only", case_id="c",
                reviewable=True),
            row("E1", P + dt.timedelta(3), "Missing", coverage="none"),
            row("E2", P, None, coverage="none", visibility="case_only"),             # withheld
            row("E3", P, "Missing", coverage="none", employment="before_join")]      # not employed
    s = m.summary(rows)
    assert s == {"employees": 1, "employee_days": 4, "source_cells": 2, "source_blank": 1,
                 "missing_source": 2, "flagged_only": 1, "no_source_no_case": 1,
                 "pending": 1, "to_confirm": 1}
    assert "2 of 4 employee-days" in m.coverage_warning(s)
    assert "not absence" in m.coverage_warning(s)


def test_no_warning_when_every_day_has_a_cell():
    assert m.coverage_warning(m.summary([row("E1", P, "Present")])) is None
    assert m.coverage_warning(m.summary([])) is None


def test_matrix_has_every_day_column_and_filters_by_category():
    days = [P + dt.timedelta(i) for i in range(30)]
    rows = [row("E1", d, "Present") for d in days] + [row("E2", d, "Missing", coverage="none")
                                                      for d in days]
    rows[3]["tl_effective"] = "Leave"
    cols, table = m.matrix(rows)
    assert len(cols) == 30 and cols[0] == "15 Sep" and cols[-1] == "14 Oct"
    assert [t["CRM"] for t in table] == ["E1", "E2"]
    assert [t["CRM"] for t in m.matrix(rows, {"Leave"})[1]] == ["E1"]
    assert [t["CRM"] for t in m.matrix(rows, {"Missing"})[1]] == ["E2"]
    assert m.matrix(rows, {"Absent"})[1] == []
    assert [t["CRM"] for t in m.matrix(rows, {"Absent", "Missing"})[1]] == ["E2"]


def test_a_withheld_day_never_matches_a_filter():
    rows = [row("E1", P, None, coverage="none", visibility="case_only")]
    assert m.matrix(rows, {"Missing"})[1] == []


def test_period_options_newest_first_across_the_year_end():
    opts = m.period_options(D(2027, 1, 3), n=3)
    assert opts == [D(2026, 12, 15), D(2026, 11, 15), D(2026, 10, 15)]
    assert m.period_options(D(2026, 10, 15), n=1) == [D(2026, 10, 15)]
    assert m.period_options(D(2026, 10, 14), n=1) == [D(2026, 9, 15)]


def test_to_confirm_lists_only_reviewable_days():
    rows = [row("E1", P, "Pending", case_id="c", reviewable=True, name="Zed"),
            row("E2", P, "Pending", case_id="d", reviewable=False)]
    assert m.to_confirm(rows) == [("Zed", P)]


def test_future_days_are_upcoming_not_missing_and_not_counted():
    today = P + dt.timedelta(9)
    rows = [row("E1", P + dt.timedelta(i), "Missing" if i > 9 else "Present",
                coverage="none" if i > 9 else "full") for i in range(30)]
    assert m.cell(rows[20], today) == m.UPCOMING and m.cell(rows[20]) == m.MISSING
    s = m.summary(rows, today)
    assert (s["employee_days"], s["missing_source"]) == (10, 0)
    assert m.coverage_warning(s) is None
    assert m.matrix(rows, {"Missing"}, today)[1] == []
    assert m.matrix(rows, None, today)[1][0][f"{P + dt.timedelta(20):%d %b}"] == m.UPCOMING
