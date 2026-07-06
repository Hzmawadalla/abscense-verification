-- Row Level Security (SPEC §7)
--
-- Model: the TL page and HRBP dashboard never talk to Postgres directly. All reads/writes go
-- through Netlify Functions authenticated with the service_role key, which BYPASSES RLS.
-- Therefore the correct posture for the anon/authenticated roles (the keys that could ever reach
-- a browser) is: zero access. We enable + FORCE RLS on every table and add NO permissive policy,
-- so PostgREST returns nothing for anon/authenticated. Defense in depth: also revoke table grants.

begin;

set search_path = attendance, public;

do $$
declare t text;
begin
  foreach t in array array[
    'hrbp_users','managers','employees','status_vocabulary','ingestion_runs',
    'cases','case_attachments','ingestion_exceptions','notifications','audit_log'
  ]
  loop
    execute format('alter table %I enable row level security;', t);
    execute format('alter table %I force  row level security;', t);
    execute format('revoke all on table %I from anon, authenticated;', t);
  end loop;
end $$;

-- No policies are defined on purpose. Every table is deny-by-default for anon/authenticated.
-- service_role (used only server-side by Netlify Functions) bypasses RLS and retains full access.
-- If a future feature needs direct client access under a Supabase Auth JWT, add scoped policies
-- here — do not grant broad access.

commit;
