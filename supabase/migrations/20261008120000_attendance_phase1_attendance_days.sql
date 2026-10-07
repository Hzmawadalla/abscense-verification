-- Phase 1: complete attendance storage.
--
-- * attendance.attendance_days — one row per upload x CRM x work_date for EVERY cell of an
--   uploaded Summary Report (normal, flagged, leave, half-day, unknown and blank), not only
--   flagged ones. Each upload's cells are kept as history.
-- * attendance.v_active_days — for each CRM + date, the cell from the NEWEST upload containing it
--   (a newer blank is still the active source). This is source resolution only; the final /
--   effective status is Phase 2.
-- * attendance.period_start(date) / period_end(date) — the Attendance Period (15th -> 14th), the
--   SQL twin of ingestion/periods.py (tests/pg asserts they agree day by day).
-- * ingestion_runs.file_sha256 + full_cells — duplicate-file warning, and which uploads carry
--   complete cells (every upload before Phase 1 has full_cells = false).
--
-- Additive: no existing table, column or constraint is changed or dropped, so the code running
-- before Phase 1 keeps working once this is applied. attendance_days references ingestion_runs
-- ON DELETE RESTRICT, so the Phase 1 remove_upload / reset_all_cases delete an upload's cells
-- first; the code running before Phase 1 never writes cells, so it never meets that rule.
--
-- SAFETY: a guard aborts before changing anything unless Phase 0 is in place and no Phase 1
-- object exists yet; a post-check verifies every object. Re-running once applied aborts.
--
-- PREFLIGHT before production (read-only; expected output below):
--   select to_regclass('attendance.attendance_days') as days, to_regclass('attendance.v_active_days') as v,
--          (select count(*) from information_schema.columns where table_schema = 'attendance'
--             and table_name = 'ingestion_runs' and column_name in ('file_sha256', 'full_cells')) as run_cols,
--          (select count(*) from information_schema.columns where table_schema = 'attendance'
--             and table_name = 'cases' and column_name = 'hrbp_override_note') as phase0;
-- Expected: days = NULL, v = NULL, run_cols = 0, phase0 = 1
begin;

set local search_path = attendance, public;

-- Guard: Phase 0 applied, Phase 1 not yet.
do $$
begin
  if not exists (select 1 from information_schema.columns where table_schema = 'attendance'
                   and table_name = 'cases' and column_name = 'hrbp_override_note') then
    raise exception 'Phase 1 precondition failed: Phase 0 (hrbp_override_note) is not applied. Nothing was changed.';
  end if;
  if to_regclass('attendance.attendance_days') is not null
     or to_regclass('attendance.v_active_days') is not null
     or exists (select 1 from information_schema.columns where table_schema = 'attendance'
                  and table_name = 'ingestion_runs' and column_name in ('file_sha256', 'full_cells'))
     or exists (select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                  where n.nspname = 'attendance' and p.proname in ('period_start', 'period_end')) then
    raise exception 'Phase 1 precondition failed: Phase 1 objects already exist (already applied?). Nothing was changed.';
  end if;
end $$;

-- Attendance Period: the 15th opens a period, the 14th closes one (global rule).
create function attendance.period_start(d date) returns date
  language sql immutable strict parallel safe
  as $fn$
    select case when extract(day from d) >= 15
                then (date_trunc('month', d) + interval '14 days')::date
                else (date_trunc('month', d) - interval '1 month' + interval '14 days')::date
           end
  $fn$;

create function attendance.period_end(d date) returns date
  language sql immutable strict parallel safe
  as $fn$
    select (attendance.period_start(d) + interval '1 month' - interval '1 day')::date
  $fn$;

alter table attendance.ingestion_runs
  add column file_sha256 text,
  add column full_cells  boolean not null default false;

create index ingestion_runs_file_sha256_idx on attendance.ingestion_runs(file_sha256);

create table attendance.attendance_days (
  id                   uuid primary key default gen_random_uuid(),
  ingestion_run_id     uuid not null references attendance.ingestion_runs(id) on delete restrict,
  crm                  text not null,       -- canonical employee CRM when known, else the sheet's
  crm_key              text not null,       -- lower(clean(crm)): the app-wide CRM join key
  employee_id          uuid references attendance.employees(id) on delete set null,
  manager_id_at_ingest uuid references attendance.managers(id) on delete set null,
  work_date            date not null,
  raw_value            text,                -- exact cell text; null = blank cell
  bucket               text not null
                       check (bucket in ('skip', 'not_verified', 'trigger', 'ignore', 'unknown', 'blank')),
  canonical_status     text,
  is_half_day          boolean not null default false,
  source_row           integer,
  created_at           timestamptz not null default now(),
  constraint attendance_days_run_crm_date_uniq unique (ingestion_run_id, crm_key, work_date),
  constraint attendance_days_blank_has_no_value check ((bucket = 'blank') = (raw_value is null))
);

create index attendance_days_crm_date_idx      on attendance.attendance_days(crm_key, work_date);
create index attendance_days_employee_date_idx on attendance.attendance_days(employee_id, work_date);
create index attendance_days_manager_date_idx  on attendance.attendance_days(manager_id_at_ingest, work_date);

alter table attendance.attendance_days enable row level security;
alter table attendance.attendance_days force  row level security;
revoke all on table attendance.attendance_days from anon, authenticated;

-- Active source: the newest upload (by upload time, then id) containing the CRM + date.
create view attendance.v_active_days with (security_invoker = true) as
select distinct on (d.crm_key, d.work_date)
       d.id, d.ingestion_run_id, d.crm, d.crm_key, d.employee_id, d.manager_id_at_ingest,
       d.work_date, d.raw_value, d.bucket, d.canonical_status, d.is_half_day, d.source_row,
       r.created_at as run_created_at, r.source_filename
  from attendance.attendance_days d
  join attendance.ingestion_runs r on r.id = d.ingestion_run_id
 order by d.crm_key, d.work_date, r.created_at desc, r.id desc;

revoke all on table attendance.v_active_days from anon, authenticated;

-- Post-check: every object present and the period functions behave at the boundaries.
do $$
begin
  if to_regclass('attendance.attendance_days') is null or to_regclass('attendance.v_active_days') is null then
    raise exception 'Phase 1 post-check failed: table or view missing';
  end if;
  if attendance.period_start(date '2026-12-15') <> date '2026-12-15'
     or attendance.period_end(date '2026-12-15') <> date '2027-01-14'
     or attendance.period_start(date '2027-01-14') <> date '2026-12-15'
     or attendance.period_start(date '2028-02-29') <> date '2028-02-15'
     or attendance.period_end(date '2028-02-29') <> date '2028-03-14' then
    raise exception 'Phase 1 post-check failed: period functions disagree with the 15th -> 14th rule';
  end if;
  if (select count(*) from information_schema.columns where table_schema = 'attendance'
        and table_name = 'ingestion_runs' and column_name in ('file_sha256', 'full_cells')) <> 2 then
    raise exception 'Phase 1 post-check failed: ingestion_runs columns missing';
  end if;
end $$;

commit;

-- ROLLBACK (preferred: revert the application code only and KEEP these objects — the code
-- running before Phase 1 ignores them). Full undo, only if unavoidable, AFTER reverting the
-- code; it deletes every stored attendance cell and the upload hashes:
--
-- begin;
-- drop view attendance.v_active_days;
-- drop table attendance.attendance_days;
-- drop index if exists attendance.ingestion_runs_file_sha256_idx;
-- alter table attendance.ingestion_runs drop column file_sha256, drop column full_cells;
-- drop function attendance.period_end(date);
-- drop function attendance.period_start(date);
-- commit;
