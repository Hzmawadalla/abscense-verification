"""Phase 3 M3: the TL team view's authorization boundary and field allowlist (approved decisions E1,
E2), at the query layer and on the rendered page. The app connects with a privileged server-side
role, so RLS does not protect these queries: authorization must come from the link's manager.
Synthetic data only, a throwaway 127.0.0.1 cluster, no real secrets."""
import datetime as dt
import importlib.util
import re
from pathlib import Path

import pytest

from app import data, security, tl_matrix
from app.attendance_view import TL_ROW_FIELDS, _WITHHELD, tl_period_attendance

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "p2_effective_m3", Path(__file__).with_name("test_phase2_effective.py"))
p2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(p2)
org = p2.org                                    # TL-A (E-1, E-2), TL-B (E-3)
_q, _load, _cell, _flag, _case, _answer = p2._q, p2._load, p2._cell, p2._flag, p2._case, p2._answer
P = p2.P

# what a withheld day may still carry: who and when, nothing about the day itself (decision E2)
IDENTITY_FIELDS = {"employee_id", "employee_crm", "employee_name", "work_date", "period_start",
                   "period_end", "visibility"}
NEVER_FIELDS = {"hrbp_override_note", "hrbp_override_by", "hrbp_override_at", "closed_by",
                "manager_comment", "leave_type", "final_leave_type", "raw_value", "source_status",
                "storage_path", "filename", "actor"}


def _rows(conn, tl, crm=None, period=P):
    return [r for r in tl_period_attendance(conn, tl, period) if crm is None or r["employee_crm"] == crm]


def test_the_allowlist_is_identity_plus_withholdable_fields_and_nothing_sensitive():
    assert set(TL_ROW_FIELDS) == IDENTITY_FIELDS | set(_WITHHELD)
    assert not NEVER_FIELDS & set(TL_ROW_FIELDS)


# ------------------------------------------------------------------ E1: overrides, own cases only
def test_e1_an_override_on_the_tls_own_case_shows_that_it_happened_and_the_label_only(conn, org):
    _load(conn, [_cell("E-1", P, "Absent", "trigger")], [_flag("E-1", P)])
    cid = _case(conn, "E-1", P)["id"]
    _answer(conn, cid, "present")
    assert data.close_case(conn, cid, "hrbp:private-actor@x.com", final_status="absent",
                           comment="hrbp-only reason text")
    case = _case(conn, "E-1", P)
    rows = _rows(conn, org["TL-A"], "E-1")
    day = next(r for r in rows if r["work_date"] == P)
    assert day["hrbp_overridden"] is True and day["hrbp_override_value"] == "Absent"
    dumped = repr(rows)
    for secret in ("hrbp-only reason text", "private-actor", "hrbp:"):
        assert secret not in dumped
    assert case["hrbp_override_at"] is not None
    assert all(case["hrbp_override_at"] not in r.values() for r in rows)      # no override time


def test_e1_e2_another_tls_overridden_case_reaches_neither_the_team_tl_nor_leaks_its_details(conn, org):
    # E-1 is on TL-A's team; one of E-1's days has a case assigned to TL-B, which HRBP overrides
    d1 = P + dt.timedelta(3)
    _load(conn, [_cell("E-1", P, "Normal", "skip"), _cell("E-1", d1, "Absent", "trigger", tl="TL-B")],
          [_flag("E-1", d1, tl="TL-B")])
    cid = _case(conn, "E-1", d1)["id"]
    _answer(conn, cid, "present", comment="tl-b private comment", actor="tl:TL-B")
    data.close_case(conn, cid, "hrbp:someone", final_status="absent", comment="override note")
    a_day = next(r for r in _rows(conn, org["TL-A"], "E-1") if r["work_date"] == d1)
    assert a_day["visibility"] == "team"
    assert (a_day["case_id"], a_day["tl_answer"], a_day["hrbp_overridden"],
            a_day["hrbp_override_value"], a_day["reviewable"]) == (None, None, False, None, False)
    for secret in ("tl-b private comment", "override note", "hrbp:someone", "tl:TL-B"):
        assert secret not in repr(_rows(conn, org["TL-A"]))
    # TL-B sees E-1 only because of that case: its own case day, and name/CRM on every other day
    b_rows = _rows(conn, org["TL-B"], "E-1")
    assert b_rows and all(r["visibility"] == "case_only" for r in b_rows)
    own = next(r for r in b_rows if r["work_date"] == d1)
    assert own["case_id"] == cid and own["hrbp_overridden"] is True
    for r in b_rows:
        if r["work_date"] != d1:
            assert {k: r[k] for k in _WITHHELD} == _WITHHELD, r["work_date"]
            assert r["employee_crm"] == "E-1" and r["employee_name"]


# ------------------------------------------------------------------ identity and period inputs
def test_the_view_is_scoped_by_the_manager_whatever_period_is_asked_for(conn, org):
    for period in (dt.date(2020, 1, 15), P, dt.date(2031, 6, 15)):
        assert {r["employee_crm"] for r in _rows(conn, org["TL-A"], period=period)} <= {"E-1", "E-2"}
        assert {r["employee_crm"] for r in _rows(conn, org["TL-B"], period=period)} <= {"E-3"}


def test_the_tl_page_reads_no_query_parameter_but_the_link():
    files = [REPO / "streamlit_app.py", *sorted((REPO / "app").glob("*.py"))]
    uses = [(f.name, u) for f in files
            for u in re.findall(r"query_params[^\n]*", f.read_text(encoding="utf-8"))]
    src = (REPO / "streamlit_app.py").read_text(encoding="utf-8")
    named = re.findall(r"query_params\.get\(\s*['\"](\w+)['\"]", src)
    assert named == ["t"] and len(uses) == 1, uses


def test_an_inactive_tl_link_is_refused(conn, org, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("TOKEN_ENC_KEY", Fernet.generate_key().decode())
    token = data.rotate_manager_link(conn, org["TL-A"], dt.datetime.now(dt.timezone.utc), signed=True)
    assert data.tl_access(conn, token).status == "ok"
    _q(conn, "update attendance.managers set active = false where id = %s", (org["TL-A"],))
    access = data.tl_access(conn, token)
    assert access.status != "ok" and access.manager is None


# ------------------------------------------------------------------ the rendered page
_REAL_SECRETS = [REPO / ".streamlit" / "secrets.toml", Path.home() / ".streamlit" / "secrets.toml"]
real_secrets = pytest.mark.skipif(any(p.exists() for p in _REAL_SECRETS),
                                  reason="a real secrets.toml exists; run from a clean worktree")


@real_secrets
def test_extra_query_parameters_cannot_widen_the_team_on_the_page(conn, pg_dsn, org, monkeypatch):
    from cryptography.fernet import Fernet
    from streamlit.testing.v1 import AppTest
    for var in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TOKEN_ENC_KEY", Fernet.generate_key().decode())
    assert "127.0.0.1" in pg_dsn
    token = data.rotate_manager_link(conn, org["TL-A"], dt.datetime.now(dt.timezone.utc), signed=True)
    at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=60)
    at.secrets["SUPABASE_DB_URL"] = pg_dsn
    at.query_params["t"] = token
    for k, v in (("manager_id", str(org["TL-B"])), ("tl", "TL-B"), ("crm", "TL-B"), ("m", "TL-B")):
        at.query_params[k] = v
    at.run()
    assert not at.exception, at.exception
    options = at.selectbox(key="tl_period").options
    assert len(options) == 6                                     # fixed choices, no free date
    period = tl_matrix.period_options(tl_matrix.business_today(), n=2)[1]
    at.selectbox(key="tl_period").set_value(period)
    at.run()
    assert not at.exception, at.exception
    assert set(at.dataframe[0].value["CRM"]) == {"E-1", "E-2"}   # TL-A's team only, never E-3


# ------------------------------------------------------------------ approved first-TL rule (2026-10-18)
def test_first_tl_rule_backfilled_days_respect_the_allowlist_and_other_tls(conn, org):
    from ingestion import loader
    from ingestion.db_psycopg import PsycopgDB
    from ingestion.reference import Employee, Manager, ReferenceData

    def upload(tl, on):
        ref = ReferenceData(managers=[Manager(tl, tl)] if tl else [],
                            employees=[Employee("E-7", name="Emp Seven", manager_crm=tl)])
        with conn.transaction():
            loader.load_reference(PsycopgDB(conn), ref, seen_on=on)
    upload(None, dt.date(2026, 10, 20))                         # no TL at the start of tracking
    _load(conn, [_cell("E-7", P, "Sick Leave - surgery", "skip", tl=None)])
    upload("TL-A", dt.date(2026, 11, 3))                        # first recorded TL
    rows = _rows(conn, org["TL-A"], "E-7")
    assert rows and all(r["visibility"] == "team" for r in rows)            # all earlier days
    day = next(r for r in rows if r["work_date"] == P)
    assert day["tl_effective"] == "Leave" and day["status"] == "Leave"      # labels only
    assert all(set(r) == set(TL_ROW_FIELDS) for r in rows)
    assert "surgery" not in repr(rows) and "Sick" not in repr(rows)
    assert "E-7" not in {r["employee_crm"] for r in _rows(conn, org["TL-B"])}
