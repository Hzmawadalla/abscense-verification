"""Legacy cell backfill: attach the attendance cells of an ORIGINAL workbook to the EXISTING upload
(ingestion run) it was loaded as, for uploads made before Phase 1 stored cells.

What it does — and nothing else:
  * parses the workbook with the live parser (same dating, duplicate-date and first-row rules),
  * proves the workbook IS that upload's file (below) or refuses before writing anything,
  * in ONE transaction: inserts the cells for that run and records full_cells / file_sha256 /
    the sheet's date range on the run row.

It never creates a run, never reloads employees/managers, and never touches cases, TL answers,
comments, timestamps, attachments, HRBP overrides, exceptions or the audit log. The cells keep the
run's ORIGINAL created_at, so active-source precedence (newest upload wins per CRM + date) is
exactly what it would have been had the cells been stored at the time.

Proof that the workbook is the upload's file (all must hold):
  1. the run exists, has no cells and full_cells = false (no rerun, no partial state);
  2. the workbook file name equals the run's recorded source_filename (necessary, not sufficient);
  3. the run created at least one case — otherwise there is nothing to verify against (refused);
  4. every case the run created matches the workbook exactly: same employee CRM and date, a flagged
     cell, and the same source text and half-day flag the case had when that run created it (the
     original value is taken from the earliest source_status_changed audit entry, if any).

manager_id_at_ingest stays NULL: the reference data used at the time was not kept, and the current
team assignment is not the historical one. employee_id is resolved by CRM (case-insensitive).
"""
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from psycopg.rows import dict_row

from .summary import ingest_summary


class BackfillRefused(Exception):
    """The workbook cannot be attached to the upload safely. Nothing was written."""


@dataclass
class BackfillPlan:
    run_id: object
    file_sha256: str
    range_start: object
    range_end: object
    cells: list = field(repr=False)
    cases_checked: int = 0
    distinct_crms: int = 0
    distinct_dates: int = 0


_RUN_SQL = """
select r.id, r.source_filename, r.full_cells,
       (select count(*) from attendance.attendance_days d where d.ingestion_run_id = r.id) as cells
  from attendance.ingestion_runs r where r.id = %s
"""

# Cases this run CREATED, with the source text / half-day flag they had at creation: a later
# upload that re-flagged the case audited the value it replaced (Phase 0), so the earliest such
# audit entry's old value is the original.
_RUN_CASES_SQL = """
select lower(e.crm) as crm_key, c.work_date,
       coalesce(first.old_value->>'source_status', c.source_status) as source_status,
       coalesce((first.old_value->>'is_half_day')::boolean, c.is_half_day) as is_half_day
  from attendance.cases c
  join attendance.employees e on e.id = c.employee_id
  left join lateral (
       select l.old_value from attendance.audit_log l
        where l.case_id = c.id and l.action = 'source_status_changed'
        order by l.created_at asc limit 1) first on true
 where c.ingestion_run_id = %s
"""

# employee_id by case-insensitive CRM, only when exactly one employee matches.
INSERT_BACKFILL_DAY = """
insert into attendance.attendance_days
  (ingestion_run_id, crm, crm_key, employee_id, manager_id_at_ingest, work_date, raw_value, bucket,
   canonical_status, is_half_day, source_row)
values
  (%s, %s, %s,
   (select min(e.id::text)::uuid from attendance.employees e where lower(e.crm) = %s
     having count(*) = 1),
   null, %s, %s, %s, %s, %s, %s)
"""

_MARK_RUN_SQL = """
update attendance.ingestion_runs
   set full_cells = true, file_sha256 = %s,
       range_start = coalesce(range_start, %s), range_end = coalesce(range_end, %s)
 where id = %s and full_cells = false
"""


def _parse(path, year):
    """The live parser, with no reference data: cells only (no employee/TL mapping, no cases)."""
    res = ingest_summary(str(path), SimpleNamespace(employees=[]), year=year)
    return res.cells, res.stats["date_range"]


def plan_backfill(conn, run_id, path, year) -> BackfillPlan:
    """Read-only. Every check that decides whether the workbook may be attached; raises
    BackfillRefused (writing nothing) on any doubt. UnsafeWorkbookError from the parser is
    turned into a refusal too."""
    path = Path(path)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_RUN_SQL, (run_id,))
        run = cur.fetchone()
        if run is None:
            raise BackfillRefused("no upload with that id")
        if run["full_cells"] or run["cells"]:
            raise BackfillRefused("this upload already has attendance cells (no rerun)")
        if (run["source_filename"] or "") != path.name:
            raise BackfillRefused("the workbook file name does not match the upload's recorded file "
                                  "name")
        cur.execute(_RUN_CASES_SQL, (run_id,))
        cases = cur.fetchall()
    if not cases:
        raise BackfillRefused("this upload created no cases, so the workbook cannot be verified "
                              "against it")
    try:
        cells, (first, last) = _parse(path, year)
    except ValueError as e:                       # incl. UnsafeWorkbookError
        raise BackfillRefused(f"the workbook cannot be parsed safely: {e}") from e

    by_key = {(c.crm_key, c.work_date): c for c in cells}
    mismatched = 0
    for case in cases:
        cell = by_key.get((case["crm_key"], case["work_date"]))
        if (cell is None or cell.bucket != "trigger" or cell.raw_value != case["source_status"]
                or bool(cell.is_half_day) != bool(case["is_half_day"])):
            mismatched += 1
    if mismatched:
        raise BackfillRefused(f"{mismatched} of {len(cases)} case(s) created by this upload do not "
                              "match the workbook (wrong file, wrong Year, or edited workbook)")
    return BackfillPlan(run_id=run["id"], file_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        range_start=first, range_end=last, cells=cells, cases_checked=len(cases),
                        distinct_crms=len({c.crm_key for c in cells}),
                        distinct_dates=len({c.work_date for c in cells}))


def apply_backfill(conn, plan: BackfillPlan) -> int:
    """Write the plan atomically: the run row is locked and re-checked inside the transaction, then
    every cell is inserted and the run marked. Returns the number of cells written."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("select full_cells, (select count(*) from attendance.attendance_days d "
                        " where d.ingestion_run_id = r.id) from attendance.ingestion_runs r "
                        "where r.id = %s for update", (plan.run_id,))
            row = cur.fetchone()
            if row is None or row[0] or row[1]:
                raise BackfillRefused("the upload changed since it was checked (cells present)")
            cur.executemany(INSERT_BACKFILL_DAY, [
                (plan.run_id, c.crm, c.crm_key, c.crm_key, c.work_date, c.raw_value, c.bucket,
                 c.canonical_status, c.is_half_day, c.source_row) for c in plan.cells])
            cur.execute(_MARK_RUN_SQL, (plan.file_sha256, plan.range_start, plan.range_end,
                                        plan.run_id))
            if cur.rowcount != 1:
                raise BackfillRefused("the upload could not be marked as complete")
    return len(plan.cells)
