"""TL Team Attendance View against real PostgreSQL (Phase 3 PR C): the TL-safe effective status,
review eligibility, privacy, scope and performance at ~350 employees. Synthetic data only; nothing
is sent."""
import datetime as dt
import importlib.util
import socket
import time
import tracemalloc
from pathlib import Path

import pytest

from app import data
from app.attendance_view import TL_ROW_FIELDS, tl_period_attendance
from app.tl_labels import TL_LABELS
from app import tl_matrix

_spec = importlib.util.spec_from_file_location(
    "p2_effective", Path(__file__).with_name("test_phase2_effective.py"))
p2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(p2)
org = p2.org                                    # TL-A (E-1, E-2), TL-B (E-3)
_q, _load, _cell, _flag, _case, _answer = p2._q, p2._load, p2._cell, p2._flag, p2._case, p2._answer
P = p2.P
D = dt.date
EFFECTIVE_LABELS = TL_LABELS | {"Pending", "Missing", "Blank"}


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    real = socket.socket.connect

    def guarded(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        assert host in ("127.0.0.1", "::1", "localhost"), f"tried to reach {host!r}"
        return real(sock, address)
    monkeypatch.setattr(socket.socket, "connect", guarded)


def _grid(conn, tl, crm):
    return {r["work_date"]: r for r in tl_period_attendance(conn, tl, P) if r["employee_crm"] == crm}


def test_tl_effective_follows_the_phase2_precedence_through_the_label_policy(conn, org):
    d = [P + dt.timedelta(i) for i in range(8)]
    _load(conn, [_cell("E-1", d[0], "Absent", "trigger"), _cell("E-1", d[1], "Absent", "trigger"),
                 _cell("E-1", d[2], "Absent", "trigger"), _cell("E-1", d[3], "Normal", "skip"),
                 _cell("E-1", d[4], None, "blank"), _cell("E-1", d[6], "Absent", "trigger"),
                 _cell("E-1", d[7], "Sick Leave - surgery", "skip")],
          [_flag("E-1", d[0]), _flag("E-1", d[1]), _flag("E-1", d[2]), _flag("E-1", d[6])])
    _answer(conn, _case(conn, "E-1", d[0])["id"], "present")                 # TL answer
    data.close_case(conn, _case(conn, "E-1", d[1])["id"], "hrbp:x", final_status="sick_leave",
                    comment="private medical note")                          # HRBP override
    _q(conn, "update attendance.cases set status = 'closed', final_status = 'absent', "
             "closed_by = 'hrbp_cutoff' where id = %s", (_case(conn, "E-1", d[6])["id"],))
    g = _grid(conn, org["TL-A"], "E-1")
    assert [g[x]["tl_effective"] for x in d] == ["Present", "Leave", "Pending", "Normal", "Blank",
                                                "Missing", "Pending", "Leave"]
    assert all(r["tl_effective"] in EFFECTIVE_LABELS for r in g.values())
    dumped = repr(tl_period_attendance(conn, org["TL-A"], P))
    for secret in ("Sick", "surgery", "private medical note", "hrbp:x", "sick_leave"):
        assert secret not in dumped


def test_an_explicit_absent_stays_absent_and_missing_is_never_absent(conn, org):
    d0, d1 = P, P + dt.timedelta(1)
    _load(conn, [_cell("E-1", d0, "Absent", "trigger")], [_flag("E-1", d0)])
    _answer(conn, _case(conn, "E-1", d0)["id"], "absent")
    g = _grid(conn, org["TL-A"], "E-1")
    assert (g[d0]["tl_effective"], g[d1]["tl_effective"]) == ("Absent", "Missing")


def test_only_own_open_cases_are_reviewable(conn, org):
    d0, d1 = P, P + dt.timedelta(1)
    _load(conn, [_cell("E-1", d0, "Absent", "trigger"), _cell("E-1", d1, "Absent", "trigger")],
          [_flag("E-1", d0), _flag("E-1", d1)])
    _answer(conn, _case(conn, "E-1", d1)["id"], "present")
    g = _grid(conn, org["TL-A"], "E-1")
    assert (g[d0]["reviewable"], g[d1]["reviewable"]) == (True, False)
    assert not any(r["reviewable"] for r in tl_period_attendance(conn, org["TL-B"], P))


def test_another_tls_case_day_shows_nothing_of_that_case(conn, org):
    d0 = P
    _load(conn, [_cell("E-3", d0, "Absent", "trigger", tl="TL-B")], [_flag("E-3", d0, tl="TL-B")])
    _answer(conn, _case(conn, "E-3", d0)["id"], "present", comment="tl-b private", actor="tl:TL-B")
    assert "E-3" not in {r["employee_crm"] for r in tl_period_attendance(conn, org["TL-A"], P)}
    assert "tl-b private" not in repr(tl_period_attendance(conn, org["TL-B"], P))


def test_every_row_carries_exactly_the_tl_contract(conn, org):
    _load(conn, [_cell("E-1", P, "Absent", "trigger")], [_flag("E-1", P)])
    rows = tl_period_attendance(conn, org["TL-A"], P)
    assert rows and all(tuple(r) == TL_ROW_FIELDS for r in rows)
    assert len({r["work_date"] for r in rows}) == 30                    # every day of the period


def test_viewing_writes_nothing(conn, org):
    _load(conn, [_cell("E-1", P, "Absent", "trigger")], [_flag("E-1", P)])
    snap = "select (select count(*) from attendance.audit_log) a, (select count(*) from " \
           "attendance.cases) c, (select count(*) from attendance.notifications) n, " \
           "(select count(*) from attendance.team_assignments) t"
    before = _q(conn, snap)
    tl_matrix.matrix(tl_period_attendance(conn, org["TL-A"], P))
    assert _q(conn, snap) == before


# ------------------------------------------------------------------ performance (~350 employees)
class CountingConn:
    """Counts every statement the TL view sends (N+1 detection)."""

    def __init__(self, conn):
        self._c, self.statements = conn, 0

    def cursor(self, *a, **kw):
        cur = self._c.cursor(*a, **kw)
        outer = self

        class Counted:
            def __enter__(self):
                cur.__enter__()
                return self

            def __exit__(self, *e):
                return cur.__exit__(*e)

            def execute(self, *a, **kw):
                outer.statements += 1
                return cur.execute(*a, **kw)

            def __getattr__(self, name):
                return getattr(cur, name)
        return Counted()


def test_performance_350_employees_one_tl(conn):
    with conn.cursor() as cur:
        cur.execute("insert into attendance.managers (crm, name) select 'TL-' || i, 'Lead ' || i "
                    "from generate_series(1, 14) i")
        cur.execute("insert into attendance.employees (crm, name, manager_id) "
                    "select 'E-' || i, 'Emp ' || i, (select id from attendance.managers "
                    "where crm = 'TL-' || (1 + i % 14)) from generate_series(1, 350) i")
        cur.execute("insert into attendance.ingestion_runs (source_filename, full_cells) "
                    "values ('perf.xlsx', true) returning id")
        run = cur.fetchone()[0]
        cur.execute("insert into attendance.attendance_days (ingestion_run_id, crm, crm_key, "
                    "employee_id, work_date, raw_value, bucket, canonical_status, is_half_day) "
                    "select %s, e.crm, lower(e.crm), e.id, %s::date + d, "
                    "case when d %% 9 = 0 then 'Absent' else 'Normal' end, "
                    "case when d %% 9 = 0 then 'trigger' else 'skip' end, "
                    "case when d %% 9 = 0 then 'Absent' else 'Normal' end, false "
                    "from attendance.employees e, generate_series(0, 29) d", (run, P))
        cur.execute("insert into attendance.cases (employee_id, manager_id, work_date, "
                    "source_status, ingestion_run_id) select employee_id, (select manager_id from "
                    "attendance.employees e where e.id = a.employee_id), work_date, 'Absent', %s "
                    "from attendance.attendance_days a where bucket = 'trigger'", (run,))
    conn.commit()
    tl = _q(conn, "select id from attendance.managers where crm = 'TL-1'")[0]["id"]
    counted = CountingConn(conn)
    tracemalloc.start()
    t0 = time.perf_counter()
    rows = tl_period_attendance(counted, tl, P)
    cols, table = tl_matrix.matrix(rows)
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    team = _q(conn, "select count(*) as n from attendance.employees where manager_id = %s", (tl,))
    assert len(table) == team[0]["n"] == 25 and len(cols) == 30
    assert counted.statements == 2                     # grid + team history: no N+1
    print(f"\nPERF tl_period_attendance+matrix: team={len(table)} employees x 30 days, "
          f"org=350x30={350 * 30} cells, statements={counted.statements}, "
          f"elapsed={elapsed * 1000:.0f} ms, peak_py_mem={peak / 1024:.0f} KiB")
    t1 = time.perf_counter()
    from app.attendance_view import hrbp_period_attendance
    org_rows = hrbp_period_attendance(conn, P)
    print(f"PERF hrbp_period_attendance (whole org): rows={len(org_rows)}, "
          f"elapsed={(time.perf_counter() - t1) * 1000:.0f} ms")
    assert len(org_rows) == 350 * 30
    assert elapsed < 5                                  # generous ceiling for a local run
    from app.attendance_view import _GRID_SQL, _TL_TEAM_SQL
    plan = _q(conn, "explain (analyze, costs off, timing on) "
                    + _GRID_SQL.format(team=_TL_TEAM_SQL),
              {"manager_id": tl, "ps": P, "pe": P + dt.timedelta(days=29)})
    lines = [r["QUERY PLAN"] for r in plan]
    print("PLAN (TL grid):\n  " + "\n  ".join(l for l in lines if "Scan" in l or "Execution" in l
                                              or "Planning" in l))
