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
     original value is taken from the earliest source_status_changed audit entry, if any);
  5. the other direction: every FLAGGED workbook cell is explained by the upload's own records —
     (a) a case this run created (checked in 4), or (b) a case an EARLIER upload created for that
     CRM + date whose source text AT THE TIME OF THIS UPLOAD is known from the audit log and equals
     the cell, or (c) an exception this run recorded for that CRM + date with the same raw value
     (unknown_employee / after_last_working_day / blocked_unmapped). Anything else is refused;
  6. no workbook date is later than the upload's own date (created_at in Africa/Cairo, the business
     time zone): an upload cannot contain future days.

History for (5b) is "known" only for uploads made at or after the first source_status_changed audit
entry (the earliest proof that source changes were being audited); before that, a later re-upload
could have changed a case's text without trace, so such overlaps are refused, not assumed.
Unflagged dates outside the span of the upload's case/exception dates are legitimate (all-Normal
days) and are not refused, but they are reported in the plan (dates_outside_evidence).

manager_id_at_ingest stays NULL: the reference data used at the time was not kept, and the current
team assignment is not the historical one. employee_id is resolved by CRM (case-insensitive).
"""
import datetime
import hashlib
import io
from dataclasses import dataclass, field
from types import SimpleNamespace

from psycopg.rows import dict_row

from .summary import ingest_summary

BUSINESS_TZ = "Africa/Cairo"
FLAGGED_EXCEPTION_REASONS = ("unknown_employee", "after_last_working_day", "blocked_unmapped")


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
    overlaps_supported: int = 0
    exceptions_matched: int = 0
    dates_outside_evidence: list = field(default_factory=list)


_RUN_SQL = f"""
select r.id, r.source_filename, r.full_cells, r.created_at,
       (r.created_at at time zone '{BUSINESS_TZ}')::date as upload_date,
       (select count(*) from attendance.attendance_days d where d.ingestion_run_id = r.id) as cells,
       (select min(l.created_at) from attendance.audit_log l
         where l.action = 'source_status_changed') as audit_from
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

# Cases created by EARLIER uploads on the workbook's dates, with every audited source change
# (oldest first), so their text at the time of this upload can be reconstructed.
_EARLIER_CASES_SQL = """
select lower(e.crm) as crm_key, c.work_date, c.source_status, c.is_half_day,
       coalesce((select jsonb_agg(jsonb_build_object(
                   'at', l.created_at, 'old', l.old_value, 'new', l.new_value)
                   order by l.created_at)
                  from attendance.audit_log l
                 where l.case_id = c.id and l.action = 'source_status_changed'), '[]'::jsonb)
         as changes
  from attendance.cases c
  join attendance.employees e on e.id = c.employee_id
  join attendance.ingestion_runs r on r.id = c.ingestion_run_id
 where r.created_at < %s and c.work_date between %s and %s
"""

_RUN_EXCEPTIONS_SQL = """
select lower(x.crm) as crm_key, x.work_date, x.raw_value, x.reason
  from attendance.ingestion_exceptions x
 where x.ingestion_run_id = %s and x.work_date is not null
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


def _value_at(case, when):
    """(source_status, is_half_day) an earlier case held at `when`, from its audited changes."""
    changes = case["changes"]
    before = [c for c in changes if datetime.datetime.fromisoformat(c["at"]) <= when]
    if before:
        v = before[-1]["new"]
    elif changes:
        v = changes[0]["old"]
    else:
        return case["source_status"], bool(case["is_half_day"])
    return v.get("source_status"), bool(v.get("is_half_day"))


def _check_cases(cases, cells):
    """(4) every case this run created matches a flagged cell exactly."""
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


def _check_flagged_cells(cur, run, cases, cells, run_exceptions):
    """(5) every flagged cell not covered by this run's own cases is explained by an exception of
    this run or an earlier case with proven history. Returns (overlaps, exceptions_matched)."""
    own = {(c["crm_key"], c["work_date"]) for c in cases}
    flagged = [c for c in cells if c.bucket == "trigger" and (c.crm_key, c.work_date) not in own]
    if not flagged:
        return 0, 0
    exc_keys = {(x["crm_key"], x["work_date"], x["raw_value"]) for x in run_exceptions
                if x["reason"] in FLAGGED_EXCEPTION_REASONS}
    dates = [c.work_date for c in flagged]
    cur.execute(_EARLIER_CASES_SQL, (run["created_at"], min(dates), max(dates)))
    earlier = {(r["crm_key"], r["work_date"]): r for r in cur.fetchall()}
    history_known = run["audit_from"] is not None and run["created_at"] >= run["audit_from"]
    overlaps = matched = unsupported = no_history = 0
    for cell in flagged:
        key = (cell.crm_key, cell.work_date)
        if (cell.crm_key, cell.work_date, cell.raw_value) in exc_keys:
            matched += 1
        elif key in earlier and not history_known:
            no_history += 1
        elif key in earlier and _value_at(earlier[key], run["created_at"]) == (
                cell.raw_value, bool(cell.is_half_day)):
            overlaps += 1
        else:
            unsupported += 1
    if unsupported or no_history:
        parts = []
        if unsupported:
            parts.append(f"{unsupported} flagged cell(s) have no matching case or exception from "
                         "this upload")
        if no_history:
            parts.append(f"{no_history} flagged cell(s) overlap a case from an earlier upload whose "
                         "text at the time of this upload cannot be proven (no audit history then)")
        raise BackfillRefused("; ".join(parts))
    return overlaps, matched


def _validate(cur, run_id, filename, cells):
    """Every check against the database, on an open dict_row cursor. Returns evidence counts;
    raises BackfillRefused with counts-only reasons (never names, CRMs or values)."""
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
    _check_cases(cases, cells)

    future = {c.work_date for c in cells if c.work_date > run["upload_date"]}
    if future:
        raise BackfillRefused(f"{len(future)} workbook date(s) are later than the upload's own date "
                              f"({run['upload_date']}, {BUSINESS_TZ})")

    cur.execute(_RUN_EXCEPTIONS_SQL, (run_id,))
    run_exceptions = cur.fetchall()
    overlaps, matched = _check_flagged_cells(cur, run, cases, cells, run_exceptions)

    span = {c["work_date"] for c in cases} | {x["work_date"] for x in run_exceptions}
    outside = sorted({c.work_date for c in cells
                      if c.work_date < min(span) or c.work_date > max(span)})
    return {"cases_checked": len(cases), "overlaps_supported": overlaps,
            "exceptions_matched": matched, "dates_outside_evidence": outside}


def plan_backfill(conn, run_id, data: bytes, filename: str, year) -> BackfillPlan:
    """Read-only. `data` is the workbook's bytes, read ONCE by the caller; the hash, the parse and
    (later) the written cells all come from them. Raises BackfillRefused on any doubt."""
    cells, (first, last) = _parse(data, year)
    with conn.cursor(row_factory=dict_row) as cur:
        evidence = _validate(cur, run_id, filename, cells)
    return BackfillPlan(run_id=run_id, filename=filename, year=year, data=data,
                        file_sha256=sha256_of(data), range_start=first, range_end=last, cells=cells,
                        distinct_crms=len({c.crm_key for c in cells}),
                        distinct_dates=len({c.work_date for c in cells}), **evidence)


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
