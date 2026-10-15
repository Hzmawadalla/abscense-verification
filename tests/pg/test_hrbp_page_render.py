"""HRBP page render (Streamlit AppTest) against a throwaway local PostgreSQL: once signed in, the
dashboard shows the four layers and the effective status, a source change is flagged for review,
and D4 CRMs are listed in Exceptions.

Sign-in is stubbed: streamlit-authenticator (unchanged by Phase 2) is replaced by a fake that marks
the session authenticated, so this test exercises the HRBP page, not the login library, and needs
no HRBP password at all.

SAFETY: same rule as test_tl_page_render — refuses to run when ANY real secrets file could be
picked up, so it can never reach a real database.
"""
import datetime
import sys
import types
from pathlib import Path

import pytest

from app import data
from ingestion import loader
from ingestion.db_psycopg import PsycopgDB
from ingestion.summary import (UNMAPPED_NO_FLAGS, UNMAPPED_NO_FLAGS_DETAIL, AttendanceCell,
                               CaseCandidate, IngestionException, IngestionResult)

REPO = Path(__file__).resolve().parents[2]
_REAL_SECRETS = [REPO / ".streamlit" / "secrets.toml", Path.home() / ".streamlit" / "secrets.toml"]
pytestmark = pytest.mark.skipif(any(p.exists() for p in _REAL_SECRETS),
                                reason="a real secrets.toml exists; run from a clean worktree")
D = datetime.date


def _cell(crm, day, raw, bucket):
    return AttendanceCell(crm=crm, crm_key=crm.lower(), work_date=day, raw_value=raw, bucket=bucket,
                          canonical_status=raw, is_half_day=False, manager_crm="TL-A",
                          source_row=4)


def _load(conn, cells, cases=(), exceptions=()):
    conn.commit()
    res = IngestionResult(cases=list(cases), cells=list(cells), exceptions=list(exceptions),
                          stats={"date_range": (min(c.work_date for c in cells),
                                                max(c.work_date for c in cells))})
    with conn.transaction():
        loader.load_ingestion(PsycopgDB(conn), res, source_filename="wb.xlsx", triggered_by="t")
    conn.commit()


def _text(at):
    parts = [m.value for m in at.markdown] + [m.value for m in at.caption]
    for kind in ("error", "warning", "info", "success"):
        parts += [m.value for m in getattr(at, kind)]
    parts += [m.value for m in at.subheader]
    return "\n".join(str(p) for p in parts)


def _fake_authenticator():
    """A stand-in for streamlit_authenticator: login() marks the session signed in."""
    import streamlit as st

    class Authenticate:
        def __init__(self, *a, **kw):
            pass

        def login(self, *a, **kw):
            st.session_state["authentication_status"] = True
            st.session_state["username"] = "hrbp@test.invalid"
            st.session_state["name"] = "Test HRBP"

        def logout(self, *a, **kw):
            pass

    return types.SimpleNamespace(Authenticate=Authenticate)


def test_hrbp_dashboard_shows_layers_review_and_d4(conn, pg_dsn, seed, monkeypatch):
    from streamlit.testing.v1 import AppTest
    for var in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "TOKEN_ENC_KEY", "SMTP_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setitem(sys.modules, "streamlit_authenticator", _fake_authenticator())
    assert "127.0.0.1" in pg_dsn                                      # local cluster only
    with conn.cursor() as cur:                                        # the page needs one HRBP row
        cur.execute("insert into attendance.hrbp_users (email, name, password_hash, active) "
                    "values ('hrbp@test.invalid', 'Test HRBP', 'not-a-hash', true)")
    conn.commit()
    day, moved = D(2026, 9, 20), D(2026, 9, 21)
    _load(conn, [_cell("E-1", day, "Absent", "trigger"), _cell("E-1", moved, "Absent", "trigger")],
          [CaseCandidate("E-1", "TL-A", day, "Absent"), CaseCandidate("E-1", "TL-A", moved, "Absent")],
          [IngestionException("E-GHOST", None, UNMAPPED_NO_FLAGS_DETAIL, UNMAPPED_NO_FLAGS)])
    _load(conn, [_cell("E-1", moved, "Normal", "skip")])              # newer upload clears it

    at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=60)
    at.secrets["SUPABASE_DB_URL"] = pg_dsn
    at.run()
    assert not at.exception, at.exception
    assert at.session_state["authentication_status"] is True

    at.selectbox[0].set_value("(all)")
    at.run()
    assert not at.exception, at.exception
    shown = _text(at)
    assert "1 case(s) shown need HRBP review" in shown
    assert "**Effective:** **Pending** (decided by case)" in shown or \
        "**Effective:** **Review required** (decided by case)" in shown
    assert "Not in HC/Structure — no flagged day (1)" in shown
    table = at.dataframe[0].value
    assert {"effective_status", "needs_review", "source_status", "active_raw",
            "manager_status"} <= set(table.columns)
    assert sorted(table["effective_status"]) == ["Pending", "Review required"]
