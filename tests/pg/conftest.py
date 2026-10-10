"""Real-PostgreSQL test harness (Phase 0).

Spins up a throwaway local PostgreSQL cluster in a temp directory, applies every migration in
supabase/migrations in order, and hands tests a psycopg connection. Nothing here can reach
Supabase: the cluster listens on 127.0.0.1 only and is deleted after the run.

Requires local PostgreSQL binaries (initdb, pg_ctl, psql). Point ATTENDANCE_PG_BIN at their
folder, e.g. a portable EDB zip unpacked under your user profile:

  ATTENDANCE_PG_BIN=C:/Users/<you>/pgsql-portable/pgsql/bin python -m pytest tests/pg

Without it these tests are skipped, so the normal suite (and CI) is unaffected.
"""
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = sorted((REPO_ROOT / "supabase" / "migrations").glob("*.sql"))
TABLES = ("attendance_days", "audit_log", "case_attachments", "cases", "notifications", "ingestion_exceptions",
          "ingestion_runs", "employees", "managers")


def _bin_dir():
    raw = os.environ.get("ATTENDANCE_PG_BIN")
    if not raw:
        return None
    d = Path(raw)
    return d if (d / ("initdb.exe" if os.name == "nt" else "initdb")).exists() else None


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(args, **kw):
    subprocess.run(args, check=True, capture_output=True, text=True, **kw)


@pytest.fixture(scope="session")
def pg_dsn(tmp_path_factory):
    bin_dir = _bin_dir()
    if bin_dir is None:
        pytest.skip("ATTENDANCE_PG_BIN not set — real-PostgreSQL tests skipped")
    base = tmp_path_factory.mktemp("pg")
    data_dir, log = base / "data", base / "server.log"
    port = _free_port()
    exe = lambda name: str(bin_dir / name)  # noqa: E731
    _run([exe("initdb"), "-D", str(data_dir), "-U", "postgres", "-A", "trust", "-E", "UTF8",
          "--no-locale"])
    # Not _run: the server inherits pg_ctl's stdout, so capturing it would wait for a pipe that
    # never closes. The server logs to `log` instead.
    subprocess.run([exe("pg_ctl"), "-D", str(data_dir), "-l", str(log), "-w", "start",
                    "-o", f"-p {port} -c listen_addresses=127.0.0.1"],
                   check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    try:
        dsn = f"postgresql://postgres@127.0.0.1:{port}/postgres"
        # Supabase provides these roles; the RLS migration revokes from them.
        _run([exe("psql"), dsn, "-v", "ON_ERROR_STOP=1", "-c",
              "create role anon; create role authenticated;"])
        for m in MIGRATIONS:
            _run([exe("psql"), dsn, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(m)])
        yield dsn
    finally:
        subprocess.run([exe("pg_ctl"), "-D", str(data_dir), "-m", "fast", "-w", "stop"],
                       capture_output=True)
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture()
def conn(pg_dsn):
    c = psycopg.connect(pg_dsn)
    with c.cursor() as cur:
        cur.execute("truncate " + ", ".join(f"attendance.{t}" for t in TABLES) + " cascade")
    c.commit()
    yield c
    c.close()


@pytest.fixture()
def seed(conn):
    """One TL, one employee and a factory for cases owned by a given upload."""
    with conn.cursor() as cur:
        cur.execute("insert into attendance.managers (crm, name) values ('TL-A', 'Lead A') "
                    "returning id")
        mgr = cur.fetchone()[0]
        cur.execute("insert into attendance.employees (crm, name, manager_id) "
                    "values ('E-1', 'Emp One', %s) returning id", (mgr,))
        emp = cur.fetchone()[0]
    conn.commit()

    def new_run(name="wb.xlsx"):
        with conn.cursor() as cur:
            cur.execute("insert into attendance.ingestion_runs (source_filename) values (%s) "
                        "returning id", (name,))
            rid = cur.fetchone()[0]
        conn.commit()
        return rid

    def new_case(run_id, day, source="Absent"):
        with conn.cursor() as cur:
            cur.execute("insert into attendance.cases "
                        "(employee_id, manager_id, work_date, source_status, ingestion_run_id) "
                        "values (%s, %s, %s, %s, %s) returning id",
                        (emp, mgr, day, source, run_id))
            cid = cur.fetchone()[0]
        conn.commit()
        return cid

    return {"manager_id": mgr, "employee_id": emp, "new_run": new_run, "new_case": new_case}
