"""Build the reconciled attendance report (design 2026-07-08).

Given the original attendance workbook (the wide Summary Report matrix) and the set of closed
verification cases, produce a two-sheet .xlsx returned as bytes:

  * "<matrix sheet>" — the original matrix with each closed case's cell overwritten by its final
    verdict label, matched by CRM x date. Only a cell that still reads Absent / No Show is
    overwritten; any other value (a leave, "Normal", a failed/pending leave) is left untouched.
  * "Changes" — one row per closed case dated within the workbook's period: CRM, Employee, Date, Before, After, TL, Closed by, Comment,
    In workbook?, Overwritten?.

Matching mirrors ingestion exactly (case-insensitive CRM key, same date-header parsing), so a cell
verified during ingestion maps back to the same cell here.
"""
import io
from typing import NamedTuple

import openpyxl

from ingestion.reference import _clean, _key
from ingestion.status_rules import TRIGGER_EXACT, _base, normalize
from ingestion.summary import date_columns
from ingestion.workbook import norm_header, resolve_sheet

CHANGES_SHEET = "Changes"
MATRIX_SHEET_HINT = "Summary Report"
CHANGES_HEADER = ["CRM", "Employee", "Date", "Before", "After", "TL", "Closed by", "Comment",
                  "In workbook?", "Overwritten?"]
OVERWRITTEN = "Yes"
SKIPPED_NOT_ABSENT = "No — cell is not Absent"
SKIPPED_NOT_IN_WORKBOOK = "No — not in workbook"


def is_overwritable(cell_value) -> bool:
    """Whether a matrix cell may take a manager's verdict: its base status is Absent or No Show.

    Suffixes such as "(HD)" or " - To be confirmed" are ignored, using the same base-status rule
    as ingestion. Anything else — a leave, "Normal", a failed/pending leave — keeps HR's value.
    """
    if cell_value is None:
        return False
    return _base(normalize(cell_value)) in TRIGGER_EXACT


LINKS_HEADER = ["TL name", "CRM", "Email", "Open cases", "Link"]


def _locate_header(ws):
    """Return (header_row_1based, crm_col_1based, header_values) or (None, None, None).

    The header row varies by export, so it is found as the first row containing a 'CRM' cell.
    """
    for r_idx, row in enumerate(ws.iter_rows(min_row=1, values_only=True), start=1):
        crm_col = next((j for j, h in enumerate(row) if norm_header(h) == "crm"), None)
        if crm_col is not None:
            return r_idx, crm_col + 1, row
    return None, None, None


class ReconcileResult(NamedTuple):
    """The reconciled .xlsx plus what happened to each case, for the Export tab's summary."""
    xlsx: bytes
    period: str              # "2026-06", or "2026-06-26_to_2026-07-25" when the file spans months
    overwritten: int         # cell was Absent / No Show and now holds the final verdict
    skipped_not_absent: int  # cell held something else (a leave, Normal...) and was kept
    not_in_workbook: int     # inside the period, but no row / column for it in this file
    other_period: int        # outside this file's dates — left out of the Changes sheet


def _period_label(dates) -> str:
    first, last = min(dates), max(dates)
    if (first.year, first.month) == (last.year, last.month):
        return f"{first.year}-{first.month:02d}"
    return f"{first.isoformat()}_to_{last.isoformat()}"


def _matrix_index(ws, year):
    """Return ({date: column}, {crm_key: row}) for the matrix, both 1-based."""
    hdr_row, crm_col, header_vals = _locate_header(ws)
    if hdr_row is None:
        raise ValueError("no 'CRM' column found in the attendance matrix")
    # same dating as ingestion (incl. the December -> January rollover), so a cross-year sheet
    # maps back to the very cells ingestion created cases from
    date_cols = {d: j + 1 for j, d in date_columns(header_vals, year)}
    if not date_cols:
        raise ValueError(f"no date columns found for year {year} in the attendance matrix")
    # first row wins, so a duplicated CRM maps to the same cell ingestion verified
    row_by_key = {}
    for r in range(hdr_row + 1, ws.max_row + 1):
        key = _key(_clean(ws.cell(row=r, column=crm_col).value))
        if key and key not in row_by_key:
            row_by_key[key] = r
    return date_cols, row_by_key


def reconcile(matrix, closed_cases, labels, year, sheet=MATRIX_SHEET_HINT,
              staff_gaps=None) -> ReconcileResult:
    """matrix: a path or a binary file-like object (e.g. a Streamlit upload).
    closed_cases: iterable of dicts with keys employee_crm, employee_name, manager_name,
    work_date (date), source_status, final_status (verdict code), closed_by, manager_comment.
    labels: {verdict_code: human_label}.

    Only cases dated within the workbook's own date columns are reconciled: closed cases
    accumulate across periods, and an older or later case has no business in this file."""
    wb = openpyxl.load_workbook(matrix)  # writable (not read_only) so cells can be overwritten
    try:
        ws = wb[resolve_sheet(wb, sheet)]
    except KeyError:
        ws = wb[wb.sheetnames[0]]
    date_cols, row_by_key = _matrix_index(ws, year)
    first, last = min(date_cols), max(date_cols)

    counts = {OVERWRITTEN: 0, SKIPPED_NOT_ABSENT: 0, SKIPPED_NOT_IN_WORKBOOK: 0}
    other_period = 0
    ch = wb.create_sheet(CHANGES_SHEET)
    ch.append(CHANGES_HEADER)
    for cs in closed_cases:
        wd = cs.get("work_date")
        if wd is None or not first <= wd <= last:
            other_period += 1
            continue
        row = row_by_key.get(_key(_clean(cs.get("employee_crm"))))
        col = date_cols.get(wd)
        code = cs.get("final_status")
        label = labels.get(code, code)
        in_workbook = row is not None and col is not None
        if not in_workbook:
            outcome = SKIPPED_NOT_IN_WORKBOOK
        elif is_overwritable(ws.cell(row=row, column=col).value):
            ws.cell(row=row, column=col).value = label
            outcome = OVERWRITTEN
        else:
            outcome = SKIPPED_NOT_ABSENT
        counts[outcome] += 1
        ch.append([
            cs.get("employee_crm"),
            cs.get("employee_name"),
            wd.isoformat(),
            cs.get("source_status"),
            label,
            cs.get("manager_name"),
            cs.get("closed_by"),
            cs.get("manager_comment"),
            "Yes" if in_workbook else "No",
            outcome,
        ])

    if staff_gaps:  # HC/Structure completeness gaps — staff not producing verifiable cases
        sg = wb.create_sheet("Staff not in Attendance")
        sg.append(["CRM", "Reason", "Detail"])
        for r in staff_gaps:
            sg.append([r.get("crm"), r.get("reason"), r.get("detail")])

    buf = io.BytesIO()
    wb.save(buf)
    return ReconcileResult(
        xlsx=buf.getvalue(),
        period=_period_label(date_cols),
        overwritten=counts[OVERWRITTEN],
        skipped_not_absent=counts[SKIPPED_NOT_ABSENT],
        not_in_workbook=counts[SKIPPED_NOT_IN_WORKBOOK],
        other_period=other_period,
    )


def build_reconciled_report(matrix, closed_cases, labels, year,
                            sheet=MATRIX_SHEET_HINT, staff_gaps=None) -> bytes:
    """The reconciled .xlsx only — see reconcile() for the counts and period."""
    return reconcile(matrix, closed_cases, labels, year, sheet=sheet, staff_gaps=staff_gaps).xlsx


def build_links_workbook(rows) -> bytes:
    """One-sheet .xlsx of TL links for manual distribution (mail-merge / copy).
    rows: iterable of dicts with keys name, crm, email, open_cases, link."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "TL links"
    ws.append(LINKS_HEADER)
    for r in rows:
        ws.append([r.get("name"), r.get("crm"), r.get("email"),
                   r.get("open_cases"), r.get("link")])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
