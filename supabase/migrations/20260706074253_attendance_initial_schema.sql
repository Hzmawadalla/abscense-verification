-- Attendance Verification System — initial schema
-- Mirrors SPEC.md §5. All access is intended to go through Netlify Functions using the
-- service_role key; RLS (next migration) denies anon/authenticated by default.

begin;

-- Isolate this system in its own schema so it coexists with other apps sharing this database.
create schema if not exists attendance;
set search_path = attendance, public;

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------
create type case_status        as enum ('open', 'manager_responded', 'closed');
create type verdict            as enum ('present', 'absent', 'leave');
create type vocab_bucket       as enum ('skip', 'not_verified', 'trigger', 'ignore');
create type notification_status as enum ('queued', 'sent', 'delivered', 'bounced', 'failed');

-- ---------------------------------------------------------------------------
-- Authorization allowlist (authN via Supabase Auth is not enough — see SPEC §5)
-- ---------------------------------------------------------------------------
create table hrbp_users (
  id         uuid primary key default gen_random_uuid(),
  email      text not null unique,
  active     boolean not null default true,
  created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Managers = Team Leaders (the verifiers), seeded from the Structure tab
-- ---------------------------------------------------------------------------
create table managers (
  id                uuid primary key default gen_random_uuid(),
  crm               text not null unique,
  name              text,
  email             text,
  access_token_hash text unique,            -- sha256(token); raw token only in emailed link
  active            boolean not null default true,
  created_at        timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Employees, seeded from the HC tab
-- ---------------------------------------------------------------------------
create table employees (
  id              uuid primary key default gen_random_uuid(),
  crm             text not null unique,
  ps_id           text,
  name            text,
  email           text,
  department      text,
  vendor          text,
  team            text,
  manager_id      uuid references managers(id) on delete set null,
  sm_crm          text,                     -- senior manager, reference only
  employee_status text,                     -- Active / Departed
  join_date       date,
  exit_date       date,
  created_at      timestamptz not null default now()
);
create index employees_manager_id_idx on employees(manager_id);

-- ---------------------------------------------------------------------------
-- Status vocabulary: data-driven parser rules (SPEC §4). HRBP can add mappings
-- without a code change. Unknown values fall through to ingestion_exceptions.
-- ---------------------------------------------------------------------------
create table status_vocabulary (
  id               uuid primary key default gen_random_uuid(),
  raw_value        text not null unique,    -- normalized key
  bucket           vocab_bucket not null,
  canonical_status text,
  note             text,
  active           boolean not null default true,
  created_at       timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Ingestion runs (one per uploaded workbook + range)
-- ---------------------------------------------------------------------------
create table ingestion_runs (
  id              uuid primary key default gen_random_uuid(),
  source_filename text,
  range_start     date,
  range_end       date,
  triggered_by    text,                     -- HRBP email
  created_count   integer not null default 0,
  skipped_count   integer not null default 0,
  exception_count integer not null default 0,
  created_at      timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Cases: one disputed employee-day
-- ---------------------------------------------------------------------------
create table cases (
  id                   uuid primary key default gen_random_uuid(),
  employee_id          uuid not null references employees(id) on delete restrict,
  manager_id           uuid references managers(id) on delete set null,   -- TL at creation time
  work_date            date not null,
  source_status        text not null,       -- normalized raw status, e.g. 'Absent'
  is_half_day          boolean not null default false,
  status               case_status not null default 'open',
  manager_status       verdict,
  leave_type           text,
  manager_comment      text,
  manager_responded_at timestamptz,
  final_status         verdict,
  final_leave_type     text,
  closed_by            text,                 -- 'hrbp' | 'hrbp_cutoff'
  closed_at            timestamptz,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  constraint cases_employee_work_date_uniq unique (employee_id, work_date)
);
create index cases_manager_status_idx on cases(manager_id, status);
create index cases_work_date_idx       on cases(work_date);

-- ---------------------------------------------------------------------------
-- Attachments (a case may have several)
-- ---------------------------------------------------------------------------
create table case_attachments (
  id           uuid primary key default gen_random_uuid(),
  case_id      uuid not null references cases(id) on delete cascade,
  storage_path text not null,
  filename     text,
  content_type text,
  size_bytes   integer,
  uploaded_at  timestamptz not null default now()
);
create index case_attachments_case_id_idx on case_attachments(case_id);

-- ---------------------------------------------------------------------------
-- Ingestion exceptions (unmapped employees / unknown statuses / garbage)
-- ---------------------------------------------------------------------------
create table ingestion_exceptions (
  id                uuid primary key default gen_random_uuid(),
  ingestion_run_id  uuid references ingestion_runs(id) on delete cascade,
  crm               text,
  work_date         date,
  raw_value         text,
  reason            text,                    -- 'unknown_status' | 'unmapped_employee' | ...
  resolved          boolean not null default false,
  resolved_by       text,
  created_at        timestamptz not null default now()
);
create index ingestion_exceptions_run_idx on ingestion_exceptions(ingestion_run_id);
create index ingestion_exceptions_open_idx on ingestion_exceptions(resolved) where resolved = false;

-- ---------------------------------------------------------------------------
-- Notifications (email deliverability safety net — SPEC §5)
-- ---------------------------------------------------------------------------
create table notifications (
  id                  uuid primary key default gen_random_uuid(),
  manager_id          uuid references managers(id) on delete set null,
  ingestion_run_id    uuid references ingestion_runs(id) on delete set null,
  channel             text not null default 'email',
  case_count          integer not null default 0,
  provider_message_id text,
  status              notification_status not null default 'queued',
  error               text,
  sent_at             timestamptz not null default now()
);
create index notifications_manager_idx on notifications(manager_id);

-- ---------------------------------------------------------------------------
-- Audit log (append-only, disputable-record grade — SPEC §5)
-- ---------------------------------------------------------------------------
create table audit_log (
  id         uuid primary key default gen_random_uuid(),
  case_id    uuid references cases(id) on delete set null,
  actor      text not null,                 -- TL token id | hrbp email | 'system'
  action     text not null,
  old_value  jsonb,
  new_value  jsonb,
  created_at timestamptz not null default now()
);
create index audit_log_case_id_idx on audit_log(case_id);

-- ---------------------------------------------------------------------------
-- keep cases.updated_at fresh (drives optimistic locking)
-- ---------------------------------------------------------------------------
create or replace function set_updated_at() returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create trigger cases_set_updated_at
  before update on cases
  for each row execute function set_updated_at();

commit;
