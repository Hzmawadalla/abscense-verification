"""Phase 3 M2: the team-assignment migration itself, on throwaway databases built from the repository
migrations. Prerequisites, atomicity, schema, privileges, seed and preservation of existing rows.
Synthetic data only; nothing here can reach Supabase (127.0.0.1 cluster from conftest)."""
import datetime as dt
import hashlib
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

import os

import psycopg
import pytest

# computed here, not imported from conftest (tests/ is not a package)
MIGRATIONS = sorted((Path(__file__).resolve().parents[2] / "supabase" / "migrations").glob("*.sql"))


def _bin_dir():
    return Path(os.environ["ATTENDANCE_PG_BIN"])           # set whenever pg_dsn exists


MIGRATION = next(m for m in MIGRATIONS if m.name.startswith("20261016120000_"))
BEFORE = [m for m in MIGRATIONS if m.name < MIGRATION.name]
# git's LF-normalised content of the reviewed migration (blob 2faf5edf…): the apply tooling pins it
REVIEWED_LF_SHA256 = "806d1168744703cdb1236116a1a0ce7704089bf4bba1fa00d09ccf9d882eccbd"


def test_the_migration_file_is_byte_identical_to_the_reviewed_one():
    data = MIGRATION.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == REVIEWED_LF_SHA256


@contextmanager
def scratch_db(pg_dsn, migrations):
    """A fresh database in the test cluster with exactly `migrations` applied."""
    name = f"m2_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        admin.execute(f'create database "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + f"/{name}"
    psql = str(_bin_dir() / "psql")
    try:
        for m in migrations:
            subprocess.run([psql, dsn, "-X", "-v", "ON_ERROR_STOP=1", "-q", "-f", str(m)],
                           check=True, capture_output=True, text=True)
        yield dsn
    finally:
        with psycopg.connect(pg_dsn, autocommit=True) as admin:
            admin.execute(f'drop database if exists "{name}" with (force)')


def _apply(dsn):
    """Run the migration exactly as the apply tooling does (psql, ON_ERROR_STOP, one transaction)."""
    return subprocess.run([str(_bin_dir() / "psql"), dsn, "-X", "-v", "ON_ERROR_STOP=1",
                           "-c", "set lock_timeout = '5s'", "-f", str(MIGRATION)],
                          capture_output=True, text=True)


def _seed_synthetic(c):
    c.execute("insert into attendance.managers (crm, name) values ('TL-A', 'A'), ('TL-B', 'B')")
    c.execute("insert into attendance.employees (crm, name, manager_id) select 'E-' || g, 'E' || g, "
              "(select id from attendance.managers where crm = case when g % 2 = 0 then 'TL-A' "
              "else 'TL-B' end) from generate_series(1, 6) g")
    c.execute("insert into attendance.employees (crm, name) values ('E-NOTL', 'no tl')")
    c.execute("insert into attendance.ingestion_runs (source_filename) values ('synthetic.xlsx')")
    c.execute("insert into attendance.cases (employee_id, manager_id, work_date, source_status) "
              "select e.id, e.manager_id, date '2026-09-20', 'Absent' from attendance.employees e "
              "where e.manager_id is not null")
    c.execute("insert into attendance.audit_log (case_id, actor, action) "
              "select id, 'system', 'created' from attendance.cases")


def _all_rows(c):
    tables = [t for (t,) in c.execute(
        "select table_name from information_schema.tables where table_schema = 'attendance' "
        "and table_type = 'BASE TABLE' and table_name <> 'team_assignments' order by 1").fetchall()]
    return {t: c.execute(f'select coalesce(jsonb_agg(to_jsonb(x) order by to_jsonb(x)::text), '
                         f"'[]') from attendance.\"{t}\" x").fetchone()[0] for t in tables}


def test_the_migration_refuses_without_phase1_and_changes_nothing(pg_dsn):
    phase1 = next(m for m in MIGRATIONS if m.name.startswith("20261008120000_"))
    with scratch_db(pg_dsn, [m for m in BEFORE if m.name < phase1.name]) as dsn:
        r = _apply(dsn)
        assert r.returncode == 3 and "Phase 1 (v_active_days) is not applied" in r.stderr
        with psycopg.connect(dsn) as c:
            assert c.execute("select to_regclass('attendance.team_assignments')").fetchone()[0] is None


def test_a_failure_inside_the_migration_rolls_everything_back(pg_dsn):
    with scratch_db(pg_dsn, BEFORE) as dsn:
        with psycopg.connect(dsn, autocommit=True) as c:
            _seed_synthetic(c)
            # a name clash makes the second CREATE INDEX fail after the table was created
            c.execute("create table attendance.team_assignments_manager_idx (x int)")
            before = _all_rows(c)
        r = _apply(dsn)
        assert r.returncode == 3 and "ERROR:" in r.stderr
        with psycopg.connect(dsn) as c:
            assert c.execute("select to_regclass('attendance.team_assignments')").fetchone()[0] is None
            assert c.execute("select to_regclass('attendance.team_assignments_id_seq')").fetchone()[0] is None
            assert _all_rows(c) == before


def test_schema_privileges_seed_and_preservation_after_a_clean_apply(pg_dsn):
    with scratch_db(pg_dsn, BEFORE) as dsn:
        with psycopg.connect(dsn, autocommit=True) as c:
            _seed_synthetic(c)
            before = _all_rows(c)
        cairo_today = dt.datetime.now(ZoneInfo("Africa/Cairo")).date()
        r = _apply(dsn)
        assert r.returncode == 0, r.stderr
        with psycopg.connect(dsn) as c:
            q = lambda sql: c.execute(sql).fetchone()[0]        # noqa: E731
            assert _all_rows(c) == before                        # every existing row untouched
            assert q("select count(*) from attendance.team_assignments") == 6       # E-NOTL not seeded
            assert q("select count(*) from attendance.team_assignments t join attendance.employees e "
                     "on e.id = t.employee_id where t.manager_id is distinct from e.manager_id") == 0
            assert q("select bool_and(source = 'migration_seed' and first_seen_on = last_seen_on "
                     f"and first_seen_on = date '{cairo_today}') from attendance.team_assignments")
            assert q("select relrowsecurity and relforcerowsecurity from pg_class "
                     "where oid = 'attendance.team_assignments'::regclass")
            assert q("select count(*) from pg_policies where tablename = 'team_assignments'") == 0
            for role in ("anon", "authenticated"):
                for priv in ("select", "insert", "update", "delete", "truncate", "references", "trigger"):
                    assert not q(f"select has_table_privilege('{role}', "
                                 f"'attendance.team_assignments', '{priv}')"), (role, priv)
                assert not q(f"select has_sequence_privilege('{role}', "
                             "'attendance.team_assignments_id_seq', 'usage')")
            assert sorted(c.execute("select confdeltype from pg_constraint where conrelid = "
                                    "'attendance.team_assignments'::regclass and contype = 'f'")
                          .fetchall()) == [("r",), ("r",)]
            for idx in ("team_assignments_employee_idx", "team_assignments_manager_idx"):
                assert q(f"select to_regclass('attendance.{idx}')") is not None
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute("update attendance.team_assignments set last_seen_on = first_seen_on - 1")
            c.rollback()
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute("insert into attendance.team_assignments (employee_id, first_seen_on, "
                          "last_seen_on, source) select id, current_date, current_date, 'manual' "
                          "from attendance.employees limit 1")
            c.rollback()
        rerun = _apply(dsn)
        assert rerun.returncode == 3 and "already exists" in rerun.stderr
        with psycopg.connect(dsn) as c:
            assert c.execute("select count(*) from attendance.team_assignments").fetchone()[0] == 6


def test_no_attendance_table_is_reachable_by_the_supabase_client_roles(pg_dsn):
    # TLs never get database credentials; the anon/authenticated API roles must not reach any
    # attendance table either (the app's privileged server connection is the only path)
    with psycopg.connect(pg_dsn) as c:
        rows = c.execute(
            "select c.relname, r.rolname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "cross join (values ('anon'), ('authenticated')) r(rolname) "
            "where n.nspname = 'attendance' and c.relkind in ('r', 'v', 'm', 'S') "
            "and (has_table_privilege(r.rolname, c.oid, 'select') "
            "  or has_table_privilege(r.rolname, c.oid, 'insert') "
            "  or has_table_privilege(r.rolname, c.oid, 'update') "
            "  or has_table_privilege(r.rolname, c.oid, 'delete'))").fetchall()
        assert rows == []
