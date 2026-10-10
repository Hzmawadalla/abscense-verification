"""Parse the 'Summary Report' matrix into verification cases (SPEC §6.2).

The sheet is wide (one column per calendar day); this unpivots it to one record per employee-day,
classifies each status via the shared rules, and emits a case only for a 'trigger' day belonging to
a mapped, active employee. Everything unroutable or unrecognized becomes an exception (never dropped,
never silently a deduction). Blocking of unmapped employees follows the 'complete Structure first'
decision."""
import datetime
import re
from dataclasses import dataclass, field

import openpyxl
from openpyxl.utils import get_column_letter

from .reference import _clean, _is_junk_crm, _key
from .status_rules import classify, normalize
from .workbook import norm_header, resolve_sheet

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _yearless_day(h):
    """(month, day) for a header that carries no year ('06-May', '6 May.'), else None."""
    if h is None or isinstance(h, (datetime.date, datetime.datetime)):
        return None
    m = re.match(r"^(\d{1,2})[-/ ]([A-Za-z]{3,9})\.?$", str(h).strip())
    if not m:
        return None
    mon = _MONTHS.get(m.group(2)[:3].lower())
    return (mon, int(m.group(1))) if mon else None


def parse_day_header(h, year):
    """Turn a date-matrix column header ('06-May', a datetime, ...) into a date, or None.

    A header without a year ('06-May') takes `year`; see date_columns for the December → January
    rollover across a whole header row."""
    if isinstance(h, datetime.datetime):
        return h.date()
    if isinstance(h, datetime.date):
        return h
    if h is None:
        return None
    yearless = _yearless_day(h)
    if yearless:
        if not year:
            return None
        try:
            return datetime.date(year, yearless[0], yearless[1])
        except ValueError:
            return None
    s = str(h).strip()
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def date_columns(header, year):
    """[(column_index, date)] for every date header in a row, in column order.

    `year` is the year of the FIRST year-less header ('15-Dec'). Year-less headers are dated in
    order, and when their month steps back across the year boundary (Dec → Jan, or Nov → Jan with
    December absent) every following year-less header moves to the next year — so a
    15-Dec … 14-Jan sheet entered as 2026 yields 2026-12-15 … 2027-01-14. Any other backwards
    step is left as-is (an out-of-order sheet keeps today's behaviour). Headers that carry their
    own year (a real date cell, '06-May-2026') are never shifted. Shared by ingestion and the
    reconciled export so both date a sheet identically."""
    out, offset, prev_month = [], 0, None
    for i, h in enumerate(header):
        yearless = _yearless_day(h)
        if yearless and year:
            mon = yearless[0]
            if prev_month is not None and mon < prev_month and mon + 12 - prev_month <= 2:
                offset += 1
            prev_month = mon
            d = parse_day_header(h, year + offset)
        else:
            d = parse_day_header(h, year)
        if d is not None:
            out.append((i, d))
    return out


class UnsafeWorkbookError(ValueError):
    """The workbook's dates cannot be resolved safely, so NOTHING may be ingested from it.

    Raised by the parser, before any database write; the message is safe to show HRBP (dates and
    column letters only, never cell contents)."""


def _cell_key(v) -> str:
    """A cell's comparable value for repeated-date checks: normalized text, '' for blank."""
    return "" if v is None or str(v).strip() == "" else normalize(v)


def resolve_date_columns(day_cols, data_rows, crm_idx):
    """Make the date columns unambiguous before anything is stored.

    Several columns resolving to one work date: if every employee row holds the same normalized
    value in all of them (blank vs non-blank counts as different), the FIRST column is kept and the
    date is reported as a duplicate; if any row differs, the upload is rejected. After that the
    kept dates must strictly increase left to right — a step backwards means the year or the
    rollover was resolved wrongly (e.g. a real 31-Dec cell followed by a year-less '1-Jan'), so it
    is rejected rather than guessed. Spanning several attendance periods is fine.

    Returns (kept [(column_index, date)], duplicates [(date, [column_index, ...])])."""
    by_date = {}
    for i, d in day_cols:
        by_date.setdefault(d, []).append(i)
    rows = [r for r in data_rows
            if crm_idx < len(r) and not _is_junk_crm(_clean(r[crm_idx]))
            and _clean(r[crm_idx])]
    duplicates, conflicts = [], []
    for d, cols in by_date.items():
        if len(cols) < 2:
            continue
        same = all(len({_cell_key(r[i] if i < len(r) else None) for i in cols}) == 1 for r in rows)
        (duplicates if same else conflicts).append((d, cols))
    letters = lambda cols: ", ".join(get_column_letter(i + 1) for i in cols)  # noqa: E731
    if conflicts:
        detail = "; ".join(f"{d:%d-%b-%Y} in columns {letters(c)}" for d, c in conflicts)
        raise UnsafeWorkbookError(
            f"the same date appears in more than one column with different values ({detail}). "
            "Nothing was loaded — fix the sheet so each date has one column, then upload again.")
    kept = [(i, d) for i, d in day_cols if by_date[d][0] == i]
    for (pi, pd), (ci, cd) in zip(kept, kept[1:]):
        if cd <= pd:
            raise UnsafeWorkbookError(
                f"the dates are not in order: column {get_column_letter(ci + 1)} resolves to "
                f"{cd:%d-%b-%Y}, after column {get_column_letter(pi + 1)} = {pd:%d-%b-%Y}. "
                "Check the Year (it is the year of the FIRST date column) and the date headers. "
                "Nothing was loaded.")
    return kept, duplicates


@dataclass
class CaseCandidate:
    employee_crm: str
    manager_crm: str
    work_date: datetime.date
    source_status: str
    is_half_day: bool = False


@dataclass
class IngestionException:
    crm: str | None
    work_date: object
    raw_value: str
    reason: str


@dataclass
class AttendanceCell:
    """One employee x date cell of the sheet, flagged or not (Phase 1 complete storage).

    bucket is classify()'s bucket, or 'blank' for an empty cell — kept on purpose, so a newer
    upload's blank is distinguishable from a date no upload covers."""
    crm: str                      # canonical employee CRM when known, else the sheet's CRM
    crm_key: str                  # _key(_clean(crm)) — the same normalisation as everywhere else
    work_date: datetime.date
    raw_value: str | None
    bucket: str
    canonical_status: str | None
    is_half_day: bool
    manager_crm: str | None       # the employee's TL in the reference used for this upload
    source_row: int               # 1-based sheet row


@dataclass
class IngestionResult:
    cases: list = field(default_factory=list)
    exceptions: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    cells: list = field(default_factory=list)


def ingest_summary(path, reference, year, sheet="Summary Report", header_row=3):
    emp_by_key = {_key(e.crm): e for e in reference.employees}

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[resolve_sheet(wb, sheet)]
        all_rows = list(ws.iter_rows(min_row=1, values_only=True))
    finally:
        wb.close()

    if not all_rows:
        return IngestionResult(stats={"rows": 0})

    # The header's position varies by export: the attendance-tool report carries title rows above
    # it (header on row 3), while the manually-prepared check sheet puts it on row 1. Rather than
    # trust a fixed header_row, locate the header as the first row containing a 'CRM' cell (scanning
    # from the hint downward first, then from the top). This tolerates both layouts.
    def _header_at(idx):
        return idx < len(all_rows) and any(norm_header(h) == "crm" for h in all_rows[idx])

    hdr_idx = next((i for i in range(header_row - 1, len(all_rows)) if _header_at(i)), None)
    if hdr_idx is None:
        hdr_idx = next((i for i in range(len(all_rows)) if _header_at(i)), None)
    if hdr_idx is None:
        raise ValueError("no 'CRM' column found in Summary Report header")

    rows = all_rows[hdr_idx:]
    header = rows[0]
    crm_idx = next(i for i, h in enumerate(header) if norm_header(h) == "crm")
    day_cols = date_columns(header, year)
    if not day_cols:
        raise ValueError("no date columns parsed — check the year / header_row")
    day_cols, dup_dates = resolve_date_columns(day_cols, rows[1:], crm_idx)

    result = IngestionResult()
    for d, cols in dup_dates:
        result.exceptions.append(IngestionException(
            None, d, f"date repeated in columns {', '.join(get_column_letter(i + 1) for i in cols)} "
                     "with identical values; the first column was used", "duplicate_date_column"))
    buckets = {"skip": 0, "not_verified": 0, "trigger": 0, "ignore": 0, "unknown": 0}

    first_row = {}                # crm_key -> sheet row of its first occurrence
    for sheet_row, r in enumerate(rows[1:], start=hdr_idx + 2):
        crm = _clean(r[crm_idx]) if crm_idx < len(r) else None
        if not crm or _is_junk_crm(crm):
            continue
        key = _key(crm)
        emp = emp_by_key.get(key)
        # First row wins, for stored cells AND cases (as in the reconciled export): a repeated row
        # of a CRM is only logged, so the active source and the case text can never disagree.
        if key in first_row:
            result.exceptions.append(IngestionException(
                crm, day_cols[0][1], f"duplicate row {sheet_row} (first at row {first_row[key]})",
                "duplicate_row"))
            continue
        first_row[key] = sheet_row
        for i, day in day_cols:
            val = r[i] if i < len(r) else None
            blank = val is None or str(val).strip() == ""
            if blank:
                cell_bucket, cell_canon, cell_hd = "blank", None, False
            else:
                cell_bucket, cell_canon, cell_hd = classify(str(val).strip())
            result.cells.append(AttendanceCell(
                crm=emp.crm if emp else crm, crm_key=key, work_date=day,
                raw_value=None if blank else str(val).strip(), bucket=cell_bucket,
                canonical_status=cell_canon, is_half_day=cell_hd,
                manager_crm=emp.manager_crm if emp else None, source_row=sheet_row))
            if blank:
                continue
            raw = str(val).strip()
            bucket, canon, is_hd = classify(raw)
            buckets[bucket] += 1
            if bucket == "unknown":
                result.exceptions.append(IngestionException(crm, day, raw, "unknown_status"))
                continue
            if bucket != "trigger":
                continue
            if emp is None:
                result.exceptions.append(IngestionException(crm, day, raw, "unknown_employee"))
            elif getattr(emp, "exit_date", None) and day > emp.exit_date:
                result.exceptions.append(IngestionException(crm, day, raw, "after_last_working_day"))
            elif emp.manager_crm is None:
                result.exceptions.append(IngestionException(crm, day, raw, "blocked_unmapped"))
            else:
                # keep the full raw text so the annotation that made it a case survives
                # (e.g. 'Annual Leave - To Be Confirmed', not just 'Annual Leave')
                result.cases.append(CaseCandidate(
                    employee_crm=emp.crm, manager_crm=emp.manager_crm, work_date=day,
                    source_status=raw, is_half_day=is_hd))

    dates = [d for _, d in day_cols]
    result.stats = {
        "cases": len(result.cases),
        "exceptions": len(result.exceptions),
        "days_by_bucket": buckets,
        "cells": len(result.cells),
        "date_range": (min(dates), max(dates)),
        "duplicate_dates": [d for d, _ in dup_dates],
    }
    return result
