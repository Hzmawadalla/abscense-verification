-- Tag each case with the upload (ingestion run) that created it, so HRBP can remove a single
-- upload's cases from the UI. SET NULL (not cascade) so a run delete never silently drops cases.
set search_path = attendance, public;

alter table attendance.cases
  add column if not exists ingestion_run_id uuid
    references attendance.ingestion_runs(id) on delete set null;

create index if not exists cases_ingestion_run_id_idx on attendance.cases(ingestion_run_id);

-- One-time backfill: each existing case -> the run active when it was created (latest run whose
-- created_at is at or before the case's). Exact for single-run data; best-effort otherwise.
update attendance.cases c
set ingestion_run_id = (
  select r.id from attendance.ingestion_runs r
  where r.created_at <= c.created_at
  order by r.created_at desc
  limit 1)
where c.ingestion_run_id is null;
