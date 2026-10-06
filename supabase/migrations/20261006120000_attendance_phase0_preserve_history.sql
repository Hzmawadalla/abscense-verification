-- Phase 0: stop losing evidence and history (docs: review of 2026-10-06).
--
-- * Evidence is soft-voided instead of deleted: a voided TL answer's attachments keep their rows
--   (and storage objects), marked voided_at / voided_by, and are no longer current evidence.
-- * A case that has attachments or audit entries can no longer be deleted out from under them:
--   both foreign keys change from CASCADE / SET NULL to RESTRICT.
-- * HRBP Override metadata lives in its own columns, so an override never overwrites the TL's
--   comment.
--
-- Additive and safe to apply BEFORE the Phase 0 code: today's code never deletes a case that has
-- attachments or audit rows except via remove_upload / reset_all_cases, which now fail with a
-- foreign-key error instead of silently deleting history.
--
-- Before applying: confirm the existing constraint names (the DROP ... IF EXISTS below would
-- silently skip a differently named constraint and leave the old CASCADE in place):
--   select conrelid::regclass, conname, confdeltype from pg_constraint
--   where conrelid in ('attendance.case_attachments'::regclass, 'attendance.audit_log'::regclass)
--     and contype = 'f';
-- Expected: case_attachments_case_id_fkey (c = cascade), audit_log_case_id_fkey (n = set null).
begin;

set search_path = attendance, public;

alter table attendance.case_attachments
  add column if not exists voided_at timestamptz,
  add column if not exists voided_by text;

create index if not exists case_attachments_active_idx
  on attendance.case_attachments(case_id) where voided_at is null;

alter table attendance.case_attachments drop constraint if exists case_attachments_case_id_fkey;
alter table attendance.case_attachments add constraint case_attachments_case_id_fkey
  foreign key (case_id) references attendance.cases(id) on delete restrict;

alter table attendance.audit_log drop constraint if exists audit_log_case_id_fkey;
alter table attendance.audit_log add constraint audit_log_case_id_fkey
  foreign key (case_id) references attendance.cases(id) on delete restrict;

alter table attendance.cases
  add column if not exists hrbp_override_note text,
  add column if not exists hrbp_override_by   text,
  add column if not exists hrbp_override_at   timestamptz;

commit;

-- ROLLBACK (preferred: revert the application code only and KEEP these columns — they are
-- harmless to the previous code, and dropping them loses void markers and override notes).
-- Full undo, only if unavoidable, AFTER reverting the code:
--
-- begin;
-- alter table attendance.case_attachments drop constraint case_attachments_case_id_fkey;
-- alter table attendance.case_attachments add constraint case_attachments_case_id_fkey
--   foreign key (case_id) references attendance.cases(id) on delete cascade;
-- alter table attendance.audit_log drop constraint audit_log_case_id_fkey;
-- alter table attendance.audit_log add constraint audit_log_case_id_fkey
--   foreign key (case_id) references attendance.cases(id) on delete set null;
-- drop index if exists attendance.case_attachments_active_idx;
-- alter table attendance.case_attachments drop column voided_at, drop column voided_by;
-- alter table attendance.cases drop column hrbp_override_note, drop column hrbp_override_by,
--   drop column hrbp_override_at;
-- commit;
