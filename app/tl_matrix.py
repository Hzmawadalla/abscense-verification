"""The TL Team Attendance matrix (Phase 3 PR C): pure shaping of tl_period_attendance rows.

Nothing here reads the database or decides a status. Every value comes from the TL-safe contract
(app.attendance_view.TL_ROW_FIELDS): `tl_effective` is the Phase 2 effective status passed through
the TL label policy, `visibility` says whether the day is on this TL's team, `employment` marks days
before a join date or after an exit date. Missing (no source cell, no case) and Blank (the newest
upload left the cell empty) are never Absent.
"""
import datetime as dt
from collections import Counter

from ingestion.periods import attendance_period

PENDING, MISSING, BLANK = "Pending", "Missing", "Blank"
WITHHELD = "—"            # not on this TL's team that day (another team, or the gap of a move)
NOT_EMPLOYED = "n/e"      # before the join date / after the exit date, with no source cell
UPCOMING = "Upcoming"     # a day after today: no attendance can exist yet

ABSENT_LABELS = frozenset({"Absent", "No Show"})
LEAVE_LABEL = "Leave"

# Filter -> which effective labels it selects.
FILTERS = {
    "Pending": {PENDING},
    "Absent": ABSENT_LABELS,
    "Missing": {MISSING},
    "Blank": {BLANK},
    "Leave": {LEAVE_LABEL},
}

LEGEND = [
    ("Present / Normal / …", "the attendance status (your answer or HRBP's decision wins over the sheet)"),
    ("Absent / No Show", "an absence that stands (sheet, your answer, or HRBP)"),
    ("Pending", "a flagged day nobody has decided yet — not an absence"),
    ("Leave", "an approved leave (the type is not shown)"),
    ("Missing", "no attendance was uploaded for that day — missing data, NOT an absence"),
    ("Blank", "the newest upload left that cell empty — NOT an absence"),
    ("Flagged — review required", "the sheet flags this day; HRBP or the case's TL will review it"),
    ("Unclassified", "a status the app can't show — ask your HRBP"),
    (UPCOMING, "a day that hasn't happened yet"),
    (WITHHELD, "not on your team that day"),
    (NOT_EMPLOYED, "before the join date or after the exit date"),
]


def _upcoming(row, today) -> bool:
    return today is not None and row["work_date"] > today


def cell(row, today=None) -> str:
    """The text of one matrix cell. `today` (business date) marks later days as Upcoming."""
    if row["visibility"] != "team" and not row["case_id"]:
        return WITHHELD
    if _upcoming(row, today) and row["coverage"] == "none":
        return UPCOMING
    label = row["tl_effective"]
    if row["employment"] and row["coverage"] == "none":
        return NOT_EMPLOYED
    return label or MISSING


def _in_scope(row, today=None) -> bool:
    """An employee-day that counts in the coverage figures: on the team (or an own case day),
    within the employment dates when they are known, and not after today."""
    return ((row["visibility"] == "team" or bool(row["case_id"])) and not row["employment"]
            and not (_upcoming(row, today) and row["coverage"] == "none"))


def summary(rows, today=None) -> dict:
    """Counts for the team summary. Denominator = employee-days in scope (_in_scope)."""
    scope = [r for r in rows if _in_scope(r, today)]
    by = Counter(r["coverage"] for r in scope)
    blank = sum(1 for r in scope if r["coverage"] == "full" and r["tl_effective"] == BLANK)
    return {
        "employees": len({r["employee_id"] for r in rows if _in_scope(r, today)}),
        "employee_days": len(scope),
        "source_cells": by["full"],
        "source_blank": blank,
        "missing_source": by["flagged_only"] + by["none"],
        "flagged_only": by["flagged_only"],
        "no_source_no_case": by["none"],
        "pending": sum(1 for r in scope if r["tl_effective"] == PENDING),
        "to_confirm": sum(1 for r in scope if r["reviewable"]),
    }


def coverage_warning(s) -> str | None:
    """A warning when days have no source cell, so missing data is never read as absence."""
    if not s["employee_days"] or not s["missing_source"]:
        return None
    share = s["missing_source"] / s["employee_days"]
    return (f"{s['missing_source']} of {s['employee_days']} employee-days ({share:.0%}) have no "
            "uploaded attendance cell. That is missing data, not absence. Older periods (before "
            "full attendance was stored) are mostly Missing or flagged-only.")


def matrix(rows, only=None, today=None):
    """(day columns, table rows) for display. `only`: set of filter names; keeps employees with at
    least one in-scope day in a selected category."""
    days = sorted({r["work_date"] for r in rows})
    by_emp = {}
    for r in rows:
        by_emp.setdefault((r["employee_crm"], r["employee_name"], r["employee_id"]), []).append(r)
    wanted = set().union(*(FILTERS[f] for f in only)) if only else None
    cols = [f"{d:%d %b}" for d in days]
    out = []
    for (crm, name, _), emp_rows in sorted(by_emp.items(), key=lambda kv: (kv[0][1] or "", kv[0][0])):
        cells = {r["work_date"]: cell(r, today) for r in emp_rows}
        if wanted is not None and not any(_in_scope(r, today) and cell(r, today) in wanted
                                          for r in emp_rows):
            continue
        out.append({"CRM": crm, "Employee": name or "",
                    **{c: cells.get(d, WITHHELD) for c, d in zip(cols, days)}})
    return cols, out


def to_confirm(rows):
    """The days this TL can still answer (their own open cases), for the review hint."""
    return sorted(((r["employee_name"] or r["employee_crm"], r["work_date"])
                   for r in rows if r["reviewable"]), key=lambda t: (t[1], t[0]))


def business_today() -> dt.date:
    """Today in the business time zone (Africa/Cairo); UTC+3 if the zone database is missing."""
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("Africa/Cairo")).date()
    except Exception:  # noqa: BLE001 — no tz database on this host
        return dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).date()


def period_options(today: dt.date, n: int = 6):
    """The current Attendance Period start and the n-1 before it, newest first."""
    start, _ = attendance_period(today)
    out = [start]
    for _ in range(n - 1):
        start, _ = attendance_period(start - dt.timedelta(days=1))
        out.append(start)
    return out
