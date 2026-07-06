"""Write parser output (reference + ingestion) into the Supabase `attendance` schema (SPEC §6.1–6.2).

The orchestration takes an injected `DB` executor so it is unit-testable without a database; the
production adapter wraps a psycopg connection. All statements are schema-qualified and idempotent:
- managers/employees upsert on CRM (never clobbering a manager's issued token),
- cases upsert on (employee, work_date) WITHOUT resetting an in-flight verification,
- exceptions are recorded per ingestion run."""
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
  (employee_id, manager_id, work_date, source_status, is_half_day)
values
  ((select id from attendance.employees where crm = %s),
   (select id from attendance.managers where crm = %s),
   %s, %s, %s)
on conflict (employee_id, work_date) do update set
  source_status = excluded.source_status,
  is_half_day = excluded.is_half_day
"""

INSERT_EXCEPTION = """
insert into attendance.ingestion_exceptions (ingestion_run_id, crm, work_date, raw_value, reason)
values (%s, %s, %s, %s, %s)
"""

INSERT_RUN = """
insert into attendance.ingestion_runs
  (source_filename, range_start, range_end, triggered_by, created_count, skipped_count, exception_count)
values (%s, %s, %s, %s, %s, %s, %s)
returning id
"""


def manager_params(m):
    return (m.crm, m.name, m.email)


def employee_params(e):
    return (e.crm, e.ps_id, e.name, e.email, e.department, e.vendor, e.team,
            e.manager_crm, e.sm_crm, e.employee_status, e.join_date, e.exit_date)


def case_params(c):
    return (c.employee_crm, c.manager_crm, c.work_date, c.source_status, c.is_half_day)


@dataclass
class LoadSummary:
    run_id: str
    managers: int
    employees: int
    cases: int
    exceptions: int


def load_reference(db: DB, reference) -> None:
    """Upsert managers then employees (managers first so the employee FK subselect resolves)."""
    db.many(UPSERT_MANAGER, [manager_params(m) for m in reference.managers])
    db.many(UPSERT_EMPLOYEE, [employee_params(e) for e in reference.employees])


def load_ingestion(db: DB, result, reference=None, source_filename=None, range_start=None,
                   range_end=None, triggered_by=None) -> LoadSummary:
    """Record the run, upsert cases, and persist exceptions linked to the run.

    Reference-level exceptions (unmapped employees, missing TLs — the 'fix Structure' worklist)
    are persisted alongside the per-day ingestion exceptions so HRBP sees the full picture."""
    ref_excs = reference.exceptions if reference is not None else []
    total_exc = len(result.exceptions) + len(ref_excs)
    run_id = db.one(INSERT_RUN, (source_filename, range_start, range_end, triggered_by,
                                 len(result.cases), 0, total_exc))[0]
    db.many(UPSERT_CASE, [case_params(c) for c in result.cases])
    exc_rows = [(run_id, e.crm, e.work_date, e.raw_value, e.reason) for e in result.exceptions]
    exc_rows += [(run_id, e.crm, None, e.detail, e.reason) for e in ref_excs]
    db.many(INSERT_EXCEPTION, exc_rows)
    return LoadSummary(run_id=run_id, managers=0, employees=0,
                       cases=len(result.cases), exceptions=total_exc)
