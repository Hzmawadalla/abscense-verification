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
-- SAFETY: the foreign keys are replaced only after a guard has verified the current schema —
-- exactly one FK on each case_id column, with the expected name and the expected old delete rule
-- (case_attachments: CASCADE, audit_log: SET NULL). Anything else aborts the whole migration with
-- an exception, so a renamed or duplicated constraint can never be skipped silently and leave the
-- old CASCADE in place. A second guard after the change verifies exactly one RESTRICT FK on each
-- column. Re-running the migration once applied therefore also aborts (rules are already RESTRICT).
--
-- REQUIRED PREFLIGHT before applying to production (read-only; expected output below):
--   select con.conrelid::regclass as tbl, con.conname, con.confdeltype
--   from pg_constraint con
--   join pg_attribute att on att.attrelid = con.conrelid and att.attnum = any(con.conkey)
--   where con.contype = 'f' and att.attname = 'case_id'
--     and con.conrelid in ('attendance.case_attachments'::regclass, 'attendance.audit_log'::regclass);
-- Expected exactly two rows:
--   attendance.case_attachments | case_attachments_case_id_fkey | c   (c = cascade)
--   attendance.audit_log        | audit_log_case_id_fkey        | n   (n = set null)
begin;

set search_path = attendance, public;

-- Guard: the schema is what this migration was written against.
do $$
declare
  spec record;
  n int;
  nm text;
  rule text;
begin
  for spec in select * from (values
      ('case_attachments', 'case_attachments_case_id_fkey', 'c'),
      ('audit_log',        'audit_log_case_id_fkey',        'n')) as s(tbl, expected_name, expected_rule)
  loop
    select count(*), min(con.conname::text), min(con.confdeltype::text)
      into n, nm, rule
      from pg_constraint con
      join pg_attribute att on att.attrelid = con.conrelid and att.attnum = any(con.conkey)
     where con.conrelid = format('attendance.%I', spec.tbl)::regclass
       and con.contype = 'f' and att.attname = 'case_id';
    if n <> 1 or nm is distinct from spec.expected_name or rule is distinct from spec.expected_rule then
      raise exception 'Phase 0 precondition failed on attendance.%: expected exactly 1 FK on case_id named % with confdeltype %, found % FK(s) (name %, confdeltype %). Nothing was changed.',
        spec.tbl, spec.expected_name, spec.expected_rule, n, nm, rule;
    end if;
  end loop;
end $$;

alter table attendance.case_attachments
  add column if not exists voided_at timestamptz,
  add column if not exists voided_by text;

create index if not exists case_attachments_active_idx
  on attendance.case_attachments(case_id) where voided_at is null;

alter table attendance.case_attachments drop constraint case_attachments_case_id_fkey;
alter table attendance.case_attachments add constraint case_attachments_case_id_fkey
  foreign key (case_id) references attendance.cases(id) on delete restrict;

alter table attendance.audit_log drop constraint audit_log_case_id_fkey;
alter table attendance.audit_log add constraint audit_log_case_id_fkey
  foreign key (case_id) references attendance.cases(id) on delete restrict;

alter table attendance.cases
  add column if not exists hrbp_override_note text,
  add column if not exists hrbp_override_by   text,
  add column if not exists hrbp_override_at   timestamptz;

-- Post-check: exactly one FK per case_id column, and it is RESTRICT (no CASCADE left behind).
do $$
declare
  t text;
  n int;
  rule text;
begin
  foreach t in array array['case_attachments', 'audit_log'] loop
    select count(*), min(con.confdeltype::text)
      into n, rule
      from pg_constraint con
      join pg_attribute att on att.attrelid = con.conrelid and att.attnum = any(con.conkey)
     where con.conrelid = format('attendance.%I', t)::regclass
       and con.contype = 'f' and att.attname = 'case_id';
    if n <> 1 or rule is distinct from 'r' then
      raise exception 'Phase 0 post-check failed on attendance.%: expected exactly 1 RESTRICT FK on case_id, found % FK(s) (confdeltype %)',
        t, n, rule;
    end if;
  end loop;
end $$;

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
