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

from .reference import _clean, _is_junk_crm, _key
from .status_rules import classify
from .workbook import norm_header

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def parse_day_header(h, year):
    """Turn a date-matrix column header ('06-May', a datetime, ...) into a date, or None."""
    if isinstance(h, datetime.datetime):
        return h.date()
    if isinstance(h, datetime.date):
        return h
    if h is None:
        return None
    s = str(h).strip()
    m = re.match(r"^(\d{1,2})[-/ ]([A-Za-z]{3,9})\.?$", s)
    if m:
        mon = _MONTHS.get(m.group(2)[:3].lower())
        if mon and year:
            try:
                return datetime.date(year, mon, int(m.group(1)))
            except ValueError:
                return None
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


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
class IngestionResult:
    cases: list = field(default_factory=list)
    exceptions: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def ingest_summary(path, reference, year, sheet="Summary Report", header_row=3):
    emp_by_key = {_key(e.crm): e for e in reference.employees}

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet]
        rows = list(ws.iter_rows(min_row=header_row, values_only=True))
    finally:
        wb.close()

    if not rows:
        return IngestionResult(stats={"rows": 0})
    header = rows[0]
    crm_idx = next((i for i, h in enumerate(header) if norm_header(h) == "crm"), None)
    if crm_idx is None:
        raise ValueError("no 'CRM' column found in Summary Report header")
    day_cols = [(i, d) for i, h in enumerate(header) if (d := parse_day_header(h, year))]
    if not day_cols:
        raise ValueError("no date columns parsed — check the year / header_row")

    result = IngestionResult()
    buckets = {"skip": 0, "not_verified": 0, "trigger": 0, "ignore": 0, "unknown": 0}

    for r in rows[1:]:
        crm = _clean(r[crm_idx]) if crm_idx < len(r) else None
        if not crm or _is_junk_crm(crm):
            continue
        for i, day in day_cols:
            val = r[i] if i < len(r) else None
            if val is None or str(val).strip() == "":
                continue
            raw = str(val).strip()
            bucket, canon, is_hd = classify(raw)
            buckets[bucket] += 1
            if bucket == "unknown":
                result.exceptions.append(IngestionException(crm, day, raw, "unknown_status"))
                continue
            if bucket != "trigger":
                continue
            emp = emp_by_key.get(_key(crm))
            if emp is None:
                result.exceptions.append(IngestionException(crm, day, raw, "unknown_employee"))
            elif emp.manager_crm is None:
                result.exceptions.append(IngestionException(crm, day, raw, "blocked_unmapped"))
            else:
                # keep the full raw text so the annotation that made it a case survives
                # (e.g. 'Annual Leave - To Be Confirmed', not just 'Annual Leave')
                result.cases.append(CaseCandidate(
                    employee_crm=emp.crm, manager_crm=emp.manager_crm, work_date=day,
                    source_status=raw, is_half_day=is_hd))

    result.stats = {
        "cases": len(result.cases),
        "exceptions": len(result.exceptions),
        "days_by_bucket": buckets,
    }
    return result
