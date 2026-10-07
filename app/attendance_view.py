"""Read-only data contract for full attendance per Attendance Period (Phase 1).

No UI and no final/effective status: each day carries its ingredients separately —
Original Attendance (the active source cell), TL Response, HRBP Override, Pending/Flagged,
Source Changed and Coverage — for the future TL full-attendance view (Phase 2/3 decide how they
combine).

tl_period_attendance  — TL-facing: the TL's team only, and leave types generalised to "Leave";
                        no raw cell text, no HRBP note/actor, no comments or evidence.
hrbp_period_attendance — HRBP-facing: exact values, all employees (or one TL's team).

SECURITY GATE (not Phase 1): TL links never expire. The non-expiring private-link model must be
reviewed before any TL full-attendance UI built on this contract is released.
"""
from psycopg.rows import dict_row

from ingestion.periods import attendance_period, period_dates
from ingestion.status_rules import classify

# The TL's team for a period: employees currently assigned to the TL, PLUS employees with a case
# assigned to the TL inside the period (so a TL keeps sight of cases they must still answer after
# an employee moves team). No team history is inferred beyond this rule.
_TEAM_SQL = """
    select e.id from attendance.employees e where e.manager_id = %(manager_id)s
    union
    select c.employee_id from attendance.cases c
     where c.manager_id = %(manager_id)s and c.work_date between %(ps)s and %(pe)s
"""

_GRID_SQL = """
with team as ({team}),
emps as (select e.* from attendance.employees e where e.id in (select id from team)),
days as (                         -- integer offsets: exact, independent of the session time zone
    select %(ps)s::date + i as work_date from generate_series(0, %(pe)s::date - %(ps)s::date) i
),
active as (                       -- v_active_days is the single definition of "active source"
    select * from attendance.v_active_days
     where work_date between %(ps)s and %(pe)s
       and crm_key in (select lower(crm) from emps)
)
select e.id as employee_id, e.crm as employee_crm, e.name as employee_name, e.team,
       e.manager_id as current_manager_id, e.join_date, e.exit_date,
       d.work_date,
       a.raw_value, a.bucket, a.canonical_status, a.is_half_day,
       a.ingestion_run_id as source_run_id, a.run_created_at as source_uploaded_at,
       c.id as case_id, c.status as case_status, c.manager_id as case_manager_id,
       c.source_status as case_source_status, c.is_half_day as case_is_half_day,
       c.manager_status as tl_answer, c.manager_responded_at as tl_answered_at,
       c.final_status, c.closed_by,
       c.hrbp_override_at, c.hrbp_override_by, c.hrbp_override_note,
       (c.manager_responded_at is not null and exists (
            select 1 from attendance.audit_log l
             where l.case_id = c.id and l.action = 'source_status_changed'
               and l.created_at > c.manager_responded_at)) as audit_change_after_response
  from emps e
  cross join days d
  left join active a on a.crm_key = lower(e.crm) and a.work_date = d.work_date
  left join attendance.cases c on c.employee_id = e.id and c.work_date = d.work_date
 order by e.name, e.crm, d.work_date
"""

# TL-facing verdict codes: legacy/override leave codes generalise like statuses do.
_LEAVE_CODES = {"annual_leave", "sick_leave", "unpaid_leave", "leave"}


def tl_safe_status(value):
    """Generalise any leave type (Sick, Bereavement, Marriage, Paternity, Annual, …) to "Leave".

    Attendance categories (Normal, Late, Absent, No Show, Half Day, Weekend, Public Holiday,
    Not Yet Hired …) are returned unchanged. "No Leave" is not a leave."""
    if value is None:
        return None
    s = str(value).strip()
    low = s.lower()
    if low in _LEAVE_CODES:
        return "leave"
    if "leave" in low and low != "no leave":
        return "Leave"
    return s


def _period(period_start):
    dates = period_dates(period_start)          # raises unless period_start is a 15th
    return dates[0], dates[-1]


def _derive(r):
    """Add the derived, still-separate ingredients to one raw grid row (no final status)."""
    has_cell = r["bucket"] is not None
    has_case = r["case_id"] is not None
    r["coverage"] = "full" if has_cell else ("flagged_only" if has_case else "none")
    r["flagged"] = (r["bucket"] == "trigger") if has_cell else has_case
    r["pending"] = has_case and r["case_status"] == "open" and r["flagged"]
    r["hrbp_overridden"] = r["hrbp_override_at"] is not None
    r["source_changed_since_response"] = bool(
        r["tl_answered_at"] is not None and (
            r["audit_change_after_response"]
            or (has_cell and r["raw_value"] != r["case_source_status"])))
    wd = r["work_date"]
    r["employment"] = ("before_join" if r["join_date"] and wd < r["join_date"]
                       else "after_exit" if r["exit_date"] and wd > r["exit_date"] else None)
    r["period_start"], r["period_end"] = attendance_period(wd)
    return r


def _grid(conn, ps, pe, team_sql, params):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_GRID_SQL.format(team=team_sql), {**params, "ps": ps, "pe": pe})
        return [_derive(dict(r)) for r in cur.fetchall()]


def hrbp_period_attendance(conn, period_start, manager_id=None):
    """Every employee (or one TL's team, by the TL team rule) x every day of the period, exact
    values. HRBP-facing only."""
    ps, pe = _period(period_start)
    if manager_id is None:
        return _grid(conn, ps, pe, "select id from attendance.employees", {})
    return _grid(conn, ps, pe, _TEAM_SQL, {"manager_id": manager_id})


_TL_FIELDS = ("employee_id", "employee_crm", "employee_name", "work_date", "period_start",
              "period_end", "bucket", "is_half_day", "coverage", "flagged", "pending", "case_id",
              "case_status", "tl_answered_at", "hrbp_overridden", "source_changed_since_response",
              "employment")


UNCLASSIFIED = "Unclassified"

# A "case-only" employee (on another TL's team, included only because a case is assigned to this TL
# in the period) is visible ONLY on those case days; every other day of theirs is withheld.
_WITHHELD = {"status": None, "bucket": None, "is_half_day": False, "coverage": "none",
             "flagged": False, "pending": False, "case_id": None, "case_status": None,
             "tl_answer": None, "tl_answered_at": None, "hrbp_overridden": False,
             "hrbp_override_value": None, "source_changed_since_response": False,
             "employment": None}


def _tl_status(r):
    """TL-facing status: classified statuses only, leave generalised; never raw cell text."""
    if r["coverage"] == "full":
        if r["bucket"] == "unknown":          # free text the rules don't know: never shown raw
            return UNCLASSIFIED
        return tl_safe_status(r["canonical_status"])
    if r["case_source_status"]:               # flagged-only: classify the case's source text too
        canonical = classify(r["case_source_status"])[1]
        return tl_safe_status(canonical) if canonical else UNCLASSIFIED
    return None


def tl_period_attendance(conn, manager_id, period_start):
    """The TL's team x every day of the 15th -> 14th period, TL-safe.

    `manager_id` MUST come from the TL's own link (data.manager_by_token), never from user input.

    Team = employees currently assigned to this TL (`visibility = "team"`, every day), plus
    employees with a case assigned to this TL in the period (`visibility = "case_only"`, ONLY those
    case days — all their other days are withheld). Exposes a whitelist only: `status` is a
    classified status with leave generalised to "Leave" (unclassified text -> "Unclassified");
    raw cell text, HRBP note/actor, comments and evidence are never returned."""
    out = []
    for r in hrbp_period_attendance(conn, period_start, manager_id=manager_id):
        row = {k: r[k] for k in _TL_FIELDS}
        row["status"] = _tl_status(r)
        row["tl_answer"] = tl_safe_status(r["tl_answer"])
        row["hrbp_override_value"] = (tl_safe_status(r["final_status"])
                                      if r["hrbp_overridden"] else None)
        member = r["current_manager_id"] == manager_id
        row["visibility"] = "team" if member else "case_only"
        if not member and not (r["case_id"] and r["case_manager_id"] == manager_id):
            row.update(_WITHHELD)
        out.append(row)
    return out
