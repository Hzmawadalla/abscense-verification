"""Write parser output (reference + ingestion) into the Supabase `attendance` schema (SPEC §6.1–6.2).

The orchestration takes an injected `DB` executor so it is unit-testable without a database; the
production adapter wraps a psycopg connection. All statements are schema-qualified and idempotent:
- managers/employees upsert on CRM (never clobbering a manager's issued token),
- cases upsert on (employee, work_date) WITHOUT resetting an in-flight verification,
- a re-upload that changes an existing case's source status is audited first, so the value it
  replaces is never lost,
- every cell of the sheet (flagged or not, blanks included) is stored per upload in
  attendance_days (Phase 1); the newest upload per CRM + date is the active source,
- exceptions are recorded per ingestion run,
- a newer upload that no longer flags a day with an existing case is audited
  ('source_unflagged', Phase 2): the case, its answer, evidence and override are left as they are
  and HRBP sees it as review-required (app.effective)."""
from dataclasses import dataclass
from typing import Protocol


class DB(Protocol):
    def one(self, sql: str, params: tuple):
        """Execute a statement expected to RETURN a single row; return that row."""
        ...

    def many(self, sql: str, rows: list) -> None:
        """Execute a statement once per parameter tuple."""
        ...


UPSERT_MANAGER = """
insert into attendance.managers (crm, name, email, active)
values (%s, %s, %s, true)
on conflict (crm) do update set
  name = excluded.name,
  email = coalesce(excluded.email, attendance.managers.email),
  active = true
"""

UPSERT_EMPLOYEE = """
insert into attendance.employees
  (crm, ps_id, name, email, department, vendor, team, manager_id, sm_crm, employee_status, join_date, exit_date)
values
  (%s, %s, %s, %s, %s, %s, %s,
   (select id from attendance.managers where crm = %s), %s, %s, %s, %s)
on conflict (crm) do update set
  ps_id = excluded.ps_id, name = excluded.name, email = excluded.email,
  department = excluded.department, vendor = excluded.vendor, team = excluded.team,
  manager_id = excluded.manager_id, sm_crm = excluded.sm_crm,
  employee_status = excluded.employee_status, join_date = excluded.join_date, exit_date = excluded.exit_date
"""

# Upsert the ingested facts only; never touch verification state on an existing case.
UPSERT_CASE = """
insert into attendance.cases
  (employee_id, manager_id, work_date, source_status, is_half_day, ingestion_run_id)
values
  ((select id from attendance.employees where crm = %s),
   (select id from attendance.managers where crm = %s),
   %s, %s, %s, %s)
on conflict (employee_id, work_date) do update set
  source_status = excluded.source_status,
  is_half_day = excluded.is_half_day
"""

# Runs BEFORE UPSERT_CASE, once per case candidate: when the case already exists and the new
# upload brings a different source status (or half-day flag), record the old and new values in
# the audit log. The upsert below then overwrites source_status as before; without this entry the
# value it replaces would be gone. The "old" ingestion_run_id is the upload that SET the value
# being replaced: the new run of this case's latest source_status_changed entry, or — on the first
# change — the case's owning run (cases.ingestion_run_id is creator-owned and never reassigned).
# So run1 -> run2 -> run3 audits as run1->run2, then run2->run3. The "new" one is this upload.
AUDIT_SOURCE_CHANGE = """
insert into attendance.audit_log (case_id, actor, action, old_value, new_value)
select c.id, %(actor)s, 'source_status_changed',
  jsonb_build_object('employee_crm', e.crm, 'work_date', c.work_date,
                     'source_status', c.source_status, 'is_half_day', c.is_half_day,
                     'ingestion_run_id', coalesce(
                       (select (l.new_value->>'ingestion_run_id')::uuid
                          from attendance.audit_log l
                         where l.case_id = c.id and l.action = 'source_status_changed'
                         order by l.created_at desc
                         limit 1),
                       c.ingestion_run_id)),
  jsonb_build_object('employee_crm', e.crm, 'work_date', c.work_date,
                     'source_status', %(source_status)s::text, 'is_half_day', %(is_half_day)s::boolean,
                     'ingestion_run_id', %(run_id)s::uuid)
from attendance.cases c
join attendance.employees e on e.id = c.employee_id
where e.crm = %(employee_crm)s and c.work_date = %(work_date)s::date
  and (c.source_status is distinct from %(source_status)s::text
       or c.is_half_day is distinct from %(is_half_day)s::boolean)
"""

INSERT_EXCEPTION = """
insert into attendance.ingestion_exceptions (ingestion_run_id, crm, work_date, raw_value, reason)
values (%s, %s, %s, %s, %s)
"""

INSERT_RUN = """
insert into attendance.ingestion_runs
  (source_filename, range_start, range_end, triggered_by, created_count, skipped_count, exception_count,
   file_sha256, full_cells)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
returning id
"""

# Phase 1: one row per cell of this upload. employee / TL resolve by canonical CRM, like cases;
# an unknown CRM keeps its cell with employee_id null (never shown to a TL).
INSERT_DAY = """
insert into attendance.attendance_days
  (ingestion_run_id, crm, crm_key, employee_id, manager_id_at_ingest, work_date, raw_value, bucket,
   canonical_status, is_half_day, source_row)
values
  (%s, %s, %s,
   (select id from attendance.employees where crm = %s),
   (select id from attendance.managers where crm = %s),
   %s, %s, %s, %s, %s, %s)
"""


# Runs AFTER this upload's cells are stored, once: every existing case whose day this upload now
# covers with a non-flagged cell (blank included) gets one audit entry holding the case's flagged
# text (old) and the new cell (new). Nothing on the case changes. Skipped when the case's latest
# source entry already records this same unflagged value, so re-uploading the same file doesn't
# repeat it.
AUDIT_SOURCE_UNFLAGGED = """
insert into attendance.audit_log (case_id, actor, action, old_value, new_value)
select c.id, %(actor)s, 'source_unflagged',
  jsonb_build_object('employee_crm', e.crm, 'work_date', c.work_date,
                     'source_status', c.source_status, 'is_half_day', c.is_half_day,
                     'case_status', c.status),
  jsonb_build_object('employee_crm', e.crm, 'work_date', c.work_date,
                     'raw_value', d.raw_value, 'bucket', d.bucket,
                     'ingestion_run_id', %(run_id)s::uuid)
from attendance.attendance_days d
join attendance.cases c on c.employee_id = d.employee_id and c.work_date = d.work_date
join attendance.employees e on e.id = c.employee_id
where d.ingestion_run_id = %(run_id)s::uuid
  and d.bucket <> 'trigger'
  and not exists (
    select 1 from (
      select l.action, l.new_value from attendance.audit_log l
       where l.case_id = c.id and l.action in ('source_status_changed', 'source_unflagged')
       order by l.created_at desc, l.id desc limit 1) last
     where last.action = 'source_unflagged'
       and last.new_value->>'raw_value' is not distinct from d.raw_value
       and last.new_value->>'bucket' = d.bucket)
"""


# Phase 3: after the employee upsert, compare each listed employee's TL with their latest recorded
# stretch. Same TL -> extend that stretch's last_seen_on to this upload's date. Different TL (or no
# stretch yet) -> open a new stretch seen from this date. A NULL TL (unmapped in Structure) is
# recorded too: it ends the previous team's stretch. Nothing is ever deleted or rewritten backwards.
RECORD_ASSIGNMENTS = """
with seen_on as (
  select coalesce(%(on)s::date, (now() at time zone 'Africa/Cairo')::date) as d
),
listed as (
  select e.id as employee_id, e.manager_id from attendance.employees e where e.crm = any(%(crms)s)
),
latest as (
  select distinct on (t.employee_id) t.id, t.employee_id, t.manager_id
    from attendance.team_assignments t
   where t.employee_id in (select employee_id from listed)
   order by t.employee_id, t.first_seen_on desc, t.id desc
),
extended as (
  update attendance.team_assignments t
     set last_seen_on = greatest(t.last_seen_on, (select d from seen_on))
    from listed l join latest x on x.employee_id = l.employee_id
   where t.id = x.id and x.manager_id is not distinct from l.manager_id
  returning t.id
)
insert into attendance.team_assignments (employee_id, manager_id, first_seen_on, last_seen_on, source)
select l.employee_id, l.manager_id, s.d, s.d, 'reference_upload'
  from listed l cross join seen_on s
  left join latest x on x.employee_id = l.employee_id
 where x.employee_id is null or x.manager_id is distinct from l.manager_id
"""


def manager_params(m):
    return (m.crm, m.name, m.email)


def employee_params(e):
    return (e.crm, e.ps_id, e.name, e.email, e.department, e.vendor, e.team,
            e.manager_crm, e.sm_crm, e.employee_status, e.join_date, e.exit_date)


def case_params(c, run_id):
    return (c.employee_crm, c.manager_crm, c.work_date, c.source_status, c.is_half_day, run_id)


def day_params(cell, run_id):
    return (run_id, cell.crm, cell.crm_key, cell.crm, cell.manager_crm, cell.work_date,
            cell.raw_value, cell.bucket, cell.canonical_status, cell.is_half_day, cell.source_row)


def source_change_params(c, run_id, actor):
    return {"actor": actor, "employee_crm": c.employee_crm, "work_date": c.work_date,
            "source_status": c.source_status, "is_half_day": c.is_half_day,
            "run_id": str(run_id)}


@dataclass
class LoadSummary:
    run_id: str
    managers: int
    employees: int
    cases: int
    exceptions: int


def load_reference(db: DB, reference, seen_on=None) -> None:
    """Upsert managers then employees (managers first so the employee FK subselect resolves), then
    record each employee's TL as observed by this upload (team_assignments, Phase 3).

    `seen_on` is the observation date; None = today in Africa/Cairo (tests pass a date)."""
    db.many(UPSERT_MANAGER, [manager_params(m) for m in reference.managers])
    db.many(UPSERT_EMPLOYEE, [employee_params(e) for e in reference.employees])
    crms = [e.crm for e in reference.employees]
    if crms:
        db.many(RECORD_ASSIGNMENTS, [{"crms": crms, "on": seen_on}])


def load_ingestion(db: DB, result, reference=None, source_filename=None, range_start=None,
                   range_end=None, triggered_by=None, file_sha256=None) -> LoadSummary:
    """Record the run, upsert cases, persist exceptions, then store every cell of the sheet.

    Reference-level exceptions (unmapped employees, missing TLs — the 'fix Structure' worklist)
    are persisted alongside the per-day ingestion exceptions so HRBP sees the full picture.
    The date range defaults to the sheet's own first/last date column."""
    ref_excs = reference.exceptions if reference is not None else []
    total_exc = len(result.exceptions) + len(ref_excs)
    cells = getattr(result, "cells", None) or []
    sheet_range = result.stats.get("date_range") if result.stats else None
    if sheet_range and range_start is None and range_end is None:
        range_start, range_end = sheet_range
    run_id = db.one(INSERT_RUN, (source_filename, range_start, range_end, triggered_by,
                                 len(result.cases), 0, total_exc, file_sha256, bool(cells)))[0]
    actor = f"ingest:{triggered_by or 'system'}"
    db.many(AUDIT_SOURCE_CHANGE, [source_change_params(c, run_id, actor) for c in result.cases])
    db.many(UPSERT_CASE, [case_params(c, run_id) for c in result.cases])
    exc_rows = [(run_id, e.crm, e.work_date, e.raw_value, e.reason) for e in result.exceptions]
    exc_rows += [(run_id, e.crm, None, e.detail, e.reason) for e in ref_excs]
    db.many(INSERT_EXCEPTION, exc_rows)
    db.many(INSERT_DAY, [day_params(cell, run_id) for cell in cells])
    if cells:
        db.many(AUDIT_SOURCE_UNFLAGGED, [{"actor": actor, "run_id": str(run_id)}])
    return LoadSummary(run_id=run_id, managers=0, employees=0,
                       cases=len(result.cases), exceptions=total_exc)
