-- Phase 3 (PR B): team-assignment history, so a TL's team view can respect team moves.
--
-- * attendance.team_assignments: one row per stretch of an employee under one TL, as observed by
--   reference (HC + Structure) uploads.
--   - first_seen_on is the upload date (Africa/Cairo) of the first upload that showed the
--     assignment; last_seen_on is the latest such upload.
--   - These are OBSERVATION dates, a conservative proxy for the real HR move date, never the HR
--     move date itself.
--   - manager_id NULL = the upload showed the employee with no mapped TL (nobody's team).
-- * Seed: one row per employee that has a TL today, first_seen_on = last_seen_on = the day the
--   migration runs. With the "before the first recorded move" rule, the current TL keeps the
--   employee's whole history, exactly as before this migration.
--
-- Additive: no existing table, column or constraint changes, so the code running before PR B keeps
-- working once this is applied. RLS forced, no policy, anon/authenticated revoked (same posture as
-- every attendance table). employee_id / manager_id reference ON DELETE RESTRICT: history is never
-- deleted by a cascade.
--
-- FORWARD-ONLY. Undo = `drop table attendance.team_assignments` — only BEFORE code that reads it is
-- deployed, and it loses every recorded move.
--
-- SAFETY: a guard aborts unless Phase 1 is in place and the table doesn't exist yet; a post-check
-- verifies the table, its RLS and the seed count. Re-running once applied aborts.
--
-- PREFLIGHT before production (read-only; expected output below):
--   select to_regclass('attendance.team_assignments') as t, to_regclass('attendance.v_active_days') as phase1,
--          (select count(*) from attendance.employees where manager_id is not null) as to_seed;
-- Expected: t = NULL, phase1 = attendance.v_active_days, to_seed = <the number of employees with a TL>
begin;

set local search_path = attendance, public;

do $$
begin
  if to_regclass('attendance.v_active_days') is null then
    raise exception 'Phase 3 precondition failed: Phase 1 (v_active_days) is not applied. Nothing was changed.';
  end if;
  if to_regclass('attendance.team_assignments') is not null then
    raise exception 'Phase 3 precondition failed: team_assignments already exists (already applied?). Nothing was changed.';
  end if;
end $$;

create table attendance.team_assignments (
  id             bigserial primary key,            -- also the tie-break for same-day changes
  employee_id    uuid not null references attendance.employees(id) on delete restrict,
  manager_id     uuid references attendance.managers(id) on delete restrict,
  first_seen_on  date not null,
  last_seen_on   date not null,
  source         text not null check (source in ('migration_seed', 'reference_upload')),
  recorded_at    timestamptz not null default now(),
  constraint team_assignments_seen_order check (last_seen_on >= first_seen_on)
);
create index team_assignments_employee_idx
  on attendance.team_assignments(employee_id, first_seen_on, id);
create index team_assignments_manager_idx on attendance.team_assignments(manager_id);

alter table attendance.team_assignments enable row level security;
alter table attendance.team_assignments force  row level security;
revoke all on table attendance.team_assignments from anon, authenticated;
revoke all on sequence attendance.team_assignments_id_seq from anon, authenticated;

insert into attendance.team_assignments (employee_id, manager_id, first_seen_on, last_seen_on, source)
select e.id, e.manager_id, (now() at time zone 'Africa/Cairo')::date,
       (now() at time zone 'Africa/Cairo')::date, 'migration_seed'
  from attendance.employees e
 where e.manager_id is not null;

-- Post-check: the table, forced RLS, and one seed row per employee with a TL.
do $$
declare seeded bigint; expected bigint;
begin
  select count(*) into seeded from attendance.team_assignments where source = 'migration_seed';
  select count(*) into expected from attendance.employees where manager_id is not null;
  if seeded <> expected then
    raise exception 'Phase 3 post-check failed: seeded % rows, expected %', seeded, expected;
  end if;
  if not exists (select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
                  where n.nspname = 'attendance' and c.relname = 'team_assignments'
                    and c.relrowsecurity and c.relforcerowsecurity) then
    raise exception 'Phase 3 post-check failed: RLS is not forced on team_assignments';
  end if;
  if to_regclass('attendance.team_assignments_employee_idx') is null
     or to_regclass('attendance.team_assignments_manager_idx') is null
     or not exists (select 1 from pg_constraint where conname = 'team_assignments_seen_order')
     or has_table_privilege('anon', 'attendance.team_assignments', 'select') then
    raise exception 'Phase 3 post-check failed: an index, the check constraint or the revoke is missing';
  end if;
end $$;

commit;
