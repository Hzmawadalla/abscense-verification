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

Approval is bound to BYTES: the workbook is read once by the caller, and the hash, the parse and the
written cells all come from those same bytes. apply_backfill re-derives everything from the plan's
bytes, refuses unless their hash equals the approved one, and re-runs every check under a row lock,
so neither a swapped file nor a stale or edited plan can be written.

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
import io
from dataclasses import dataclass, field
from types import SimpleNamespace

from psycopg.rows import dict_row

from .summary import ingest_summary


class BackfillRefused(Exception):
    """The workbook cannot be attached to the upload safely. Nothing was written."""


@dataclass
class BackfillPlan:
    run_id: object
    filename: str
    year: int
    data: bytes = field(repr=False)
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


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse(data: bytes, year):
    """The live parser on the given bytes, with no reference data: cells only."""
    try:
        res = ingest_summary(io.BytesIO(data), SimpleNamespace(employees=[]), year=year)
    except (ValueError, KeyError) as e:          # incl. UnsafeWorkbookError, missing sheet
        raise BackfillRefused(f"the workbook cannot be parsed safely: {e}") from e
    return res.cells, res.stats["date_range"]


def _validate(cur, run_id, filename, cells):
    """Every check against the database, on an open cursor. Returns the number of cases checked."""
    cur.execute(_RUN_SQL, (run_id,))
    run = cur.fetchone()
    if run is None:
        raise BackfillRefused("no upload with that id")
    if run["full_cells"] or run["cells"]:
        raise BackfillRefused("this upload already has attendance cells (no rerun)")
    if (run["source_filename"] or "") != filename:
        raise BackfillRefused("the workbook file name does not match the upload's recorded file "
                              "name")
    cur.execute(_RUN_CASES_SQL, (run_id,))
    cases = cur.fetchall()
    if not cases:
        raise BackfillRefused("this upload created no cases, so the workbook cannot be verified "
                              "against it")
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
    return len(cases)


def plan_backfill(conn, run_id, data: bytes, filename: str, year) -> BackfillPlan:
    """Read-only. `data` is the workbook's bytes, read ONCE by the caller; the hash, the parse and
    (later) the written cells all come from them. Raises BackfillRefused on any doubt."""
    cells, (first, last) = _parse(data, year)
    with conn.cursor(row_factory=dict_row) as cur:
        checked = _validate(cur, run_id, filename, cells)
    return BackfillPlan(run_id=run_id, filename=filename, year=year, data=data,
                        file_sha256=sha256_of(data), range_start=first, range_end=last, cells=cells,
                        cases_checked=checked, distinct_crms=len({c.crm_key for c in cells}),
                        distinct_dates=len({c.work_date for c in cells}))


def apply_backfill(conn, plan: BackfillPlan, expect_sha256: str) -> int:
    """Write atomically. Refuses unless the plan's BYTES hash to the approved `expect_sha256`; then,
    inside one transaction holding the run row lock, re-parses those bytes and re-runs every check
    before inserting the re-parsed cells (plan.cells is never trusted for the write)."""
    if not expect_sha256 or sha256_of(plan.data) != expect_sha256:
        raise BackfillRefused("the workbook bytes do not match the approved sha256")
    cells, (first, last) = _parse(plan.data, plan.year)
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("select id from attendance.ingestion_runs where id = %s for update",
                        (plan.run_id,))
            if cur.fetchone() is None:
                raise BackfillRefused("no upload with that id")
            try:
                _validate(cur, plan.run_id, plan.filename, cells)
            except BackfillRefused as e:
                raise BackfillRefused(f"re-check under lock failed: {e}") from e
            cur.executemany(INSERT_BACKFILL_DAY, [
                (plan.run_id, c.crm, c.crm_key, c.crm_key, c.work_date, c.raw_value, c.bucket,
                 c.canonical_status, c.is_half_day, c.source_row) for c in cells])
            cur.execute(_MARK_RUN_SQL, (expect_sha256, first, last, plan.run_id))
            if cur.rowcount != 1:
                raise BackfillRefused("the upload could not be marked as complete")
    return len(cells)
