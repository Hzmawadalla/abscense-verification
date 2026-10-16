"""Team-assignment history against real PostgreSQL (Phase 3 PR B): the migration, recording from
reference uploads, and the TL team view respecting moves. Synthetic data only."""
import datetime as dt
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from app.attendance_view import tl_period_attendance
from ingestion import loader
from ingestion.db_psycopg import PsycopgDB
from ingestion.reference import Employee, Manager, ReferenceData

D = dt.date
P = D(2026, 9, 15)                       # period 15 Sep -> 14 Oct
MIGRATION = (Path(__file__).resolve().parents[2] / "supabase" / "migrations"
             / "20261016120000_attendance_phase3_team_assignments.sql")


def _q(conn, sql, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall() if cur.description else None
    conn.commit()
    return rows


def _ref(assign):
    """assign: {employee_crm: tl_crm or None}."""
    tls = sorted({t for t in assign.values() if t})
    return ReferenceData(managers=[Manager(t, t) for t in tls],
                         employees=[Employee(e, name=e, manager_crm=t) for e, t in assign.items()])


def _upload(conn, assign, on):
    with conn.transaction():
        loader.load_reference(PsycopgDB(conn), _ref(assign), seen_on=on)


def _stretches(conn, crm):
    return [(r["tl"], r["first_seen_on"], r["last_seen_on"]) for r in _q(
        conn, "select m.crm as tl, t.first_seen_on, t.last_seen_on from attendance.team_assignments t "
              "join attendance.employees e on e.id = t.employee_id "
              "left join attendance.managers m on m.id = t.manager_id "
              "where e.crm = %s order by t.first_seen_on, t.id", (crm,))]


def _tl(conn, crm):
    return _q(conn, "select id from attendance.managers where crm = %s", (crm,))[0]["id"]


def _team_days(conn, tl_crm, crm, period=P):
    return {r["work_date"] for r in tl_period_attendance(conn, _tl(conn, tl_crm), period)
            if r["employee_crm"] == crm and r["visibility"] == "team"}


def _period_days(period=P):
    return [period + dt.timedelta(i) for i in range(30)]


# ------------------------------------------------------------------ recording
def test_the_first_upload_opens_a_stretch_and_later_ones_extend_it(conn):
    _upload(conn, {"E-1": "TL-A"}, D(2026, 9, 1))
    _upload(conn, {"E-1": "TL-A"}, D(2026, 9, 20))
    _upload(conn, {"E-1": "TL-A"}, D(2026, 9, 20))          # same file again: no new row
    assert _stretches(conn, "E-1") == [("TL-A", D(2026, 9, 1), D(2026, 9, 20))]


def test_a_move_opens_a_new_stretch_and_keeps_the_old_one(conn):
    _upload(conn, {"E-1": "TL-A"}, D(2026, 9, 1))
    _upload(conn, {"E-1": "TL-A"}, D(2026, 9, 25))
    _upload(conn, {"E-1": "TL-B"}, D(2026, 10, 3))
    _upload(conn, {"E-1": "TL-B"}, D(2026, 10, 9))
    assert _stretches(conn, "E-1") == [("TL-A", D(2026, 9, 1), D(2026, 9, 25)),
                                       ("TL-B", D(2026, 10, 3), D(2026, 10, 9))]


def test_an_unmapped_upload_is_recorded_as_no_team(conn):
    _upload(conn, {"E-1": "TL-A"}, D(2026, 9, 1))
    _upload(conn, {"E-1": None}, D(2026, 9, 10))
    assert _stretches(conn, "E-1") == [("TL-A", D(2026, 9, 1), D(2026, 9, 1)),
                                       (None, D(2026, 9, 10), D(2026, 9, 10))]


def test_an_older_date_never_moves_last_seen_backwards(conn):
    _upload(conn, {"E-1": "TL-A"}, D(2026, 9, 20))
    _upload(conn, {"E-1": "TL-A"}, D(2026, 9, 5))
    assert _stretches(conn, "E-1") == [("TL-A", D(2026, 9, 20), D(2026, 9, 20))]


def test_only_listed_employees_are_recorded(conn):
    _upload(conn, {"E-1": "TL-A", "E-2": "TL-A"}, D(2026, 9, 1))
    _upload(conn, {"E-1": "TL-B"}, D(2026, 9, 10))           # E-2 not in this file
    assert _stretches(conn, "E-2") == [("TL-A", D(2026, 9, 1), D(2026, 9, 1))]


# ------------------------------------------------------------------ TL view
def test_before_any_recorded_move_the_current_tl_sees_the_whole_period(conn):
    _upload(conn, {"E-1": "TL-A"}, D(2026, 10, 16))          # history starts after the period
    assert _team_days(conn, "TL-A", "E-1") == set(_period_days())


def test_after_a_move_old_and_new_tl_each_see_only_their_days(conn):
    _upload(conn, {"E-1": "TL-A"}, D(2026, 9, 1))
    _upload(conn, {"E-1": "TL-A"}, D(2026, 9, 24))
    _upload(conn, {"E-1": "TL-B"}, D(2026, 10, 2))
    a, b = _team_days(conn, "TL-A", "E-1"), _team_days(conn, "TL-B", "E-1")
    assert a == {d for d in _period_days() if d <= D(2026, 9, 24)}
    assert b == {d for d in _period_days() if d >= D(2026, 10, 2)}
    rows = [r for r in tl_period_attendance(conn, _tl(conn, "TL-B"), P) if r["employee_crm"] == "E-1"]
    early = [r for r in rows if r["work_date"] < D(2026, 10, 2)]
    assert early and all(r["visibility"] == "case_only" and r["status"] is None
                         and r["coverage"] == "none" for r in early)       # withheld, not shown


def test_an_unrelated_tl_sees_nothing(conn):
    _upload(conn, {"E-1": "TL-A", "E-9": "TL-C"}, D(2026, 9, 1))
    _upload(conn, {"E-1": "TL-B", "E-9": "TL-C"}, D(2026, 10, 2))
    assert "E-1" not in {r["employee_crm"] for r in
                         tl_period_attendance(conn, _tl(conn, "TL-C"), P)}


def test_a_tl_who_lost_the_employee_before_the_period_gets_no_row(conn):
    _upload(conn, {"E-1": "TL-A"}, D(2026, 8, 1))
    _upload(conn, {"E-1": "TL-B"}, D(2026, 8, 20))
    assert "E-1" not in {r["employee_crm"] for r in
                         tl_period_attendance(conn, _tl(conn, "TL-A"), P)}


def test_the_old_tl_keeps_their_own_case_after_the_move(conn):
    _upload(conn, {"E-1": "TL-A"}, D(2026, 9, 1))
    _upload(conn, {"E-1": "TL-B"}, D(2026, 9, 20))
    _q(conn, "insert into attendance.cases (employee_id, manager_id, work_date, source_status) "
             "select e.id, m.id, %s, 'Absent' from attendance.employees e, attendance.managers m "
             "where e.crm = 'E-1' and m.crm = 'TL-A'", (D(2026, 10, 5),))
    rows = {r["work_date"]: r for r in tl_period_attendance(conn, _tl(conn, "TL-A"), P)
            if r["employee_crm"] == "E-1"}
    assert rows[D(2026, 10, 5)]["case_id"] and rows[D(2026, 10, 5)]["visibility"] == "case_only"
    assert rows[D(2026, 10, 6)]["coverage"] == "none" and rows[D(2026, 10, 6)]["status"] is None


def test_reading_the_tl_view_writes_nothing(conn):
    _upload(conn, {"E-1": "TL-A"}, D(2026, 9, 1))
    before = _q(conn, "select count(*) as n from attendance.team_assignments")[0]["n"]
    tl_period_attendance(conn, _tl(conn, "TL-A"), P)
    assert _q(conn, "select count(*) as n from attendance.team_assignments")[0]["n"] == before


# ------------------------------------------------------------------ the migration itself
def test_the_migration_refuses_to_run_twice(pg_dsn):
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        with pytest.raises(psycopg.errors.RaiseException, match="already exists"):
            c.execute(MIGRATION.read_text(encoding="utf-8"))
        c.execute("rollback")
        assert c.execute("select to_regclass('attendance.team_assignments')").fetchone()[0]


def test_the_migration_seeds_one_row_per_employee_with_a_tl_and_forces_rls(pg_dsn, conn):
    _upload(conn, {"E-1": "TL-A", "E-2": "TL-B", "E-3": None}, D(2026, 9, 1))
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        c.execute("drop table attendance.team_assignments")          # recreate it from the file
        c.execute(MIGRATION.read_text(encoding="utf-8"))
        rows = c.execute("select e.crm, t.source, t.first_seen_on = t.last_seen_on "
                         "from attendance.team_assignments t join attendance.employees e "
                         "on e.id = t.employee_id order by e.crm").fetchall()
        assert rows == [("E-1", "migration_seed", True), ("E-2", "migration_seed", True)]
        assert c.execute("select relrowsecurity and relforcerowsecurity from pg_class "
                         "where oid = 'attendance.team_assignments'::regclass").fetchone()[0]


def test_history_rows_are_protected_from_cascading_deletes(conn):
    _upload(conn, {"E-1": "TL-A"}, D(2026, 9, 1))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with conn.transaction():
            conn.execute("delete from attendance.employees where crm = 'E-1'")
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            conn.execute("update attendance.team_assignments set last_seen_on = first_seen_on - 1")
