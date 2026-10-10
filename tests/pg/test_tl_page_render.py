"""End-to-end TL page render (Streamlit AppTest) against a throwaway local PostgreSQL: whatever the
workbook text, the rendered TL page shows approved labels only.

SAFETY: the app reads its database from Streamlit secrets. This test refuses to run when ANY real
secrets file could be picked up (repo .streamlit/secrets.toml or ~/.streamlit/secrets.toml), so it
can never reach a real database. Run it from a clean checkout (e.g. a `git worktree`), where the
gitignored secrets file does not exist.
"""
from pathlib import Path

import pytest

from app import security

REPO = Path(__file__).resolve().parents[2]
_REAL_SECRETS = [REPO / ".streamlit" / "secrets.toml", Path.home() / ".streamlit" / "secrets.toml"]
pytestmark = pytest.mark.skipif(any(p.exists() for p in _REAL_SECRETS),
                                reason="a real secrets.toml exists; run from a clean worktree")

SENSITIVE = ["Hospital visit - check", "Surgery (Pending)", "Bereavement Leave - To Be Confirmed",
             "Annual Leave (Failed)"]


def test_rendered_tl_page_contains_labels_only(conn, pg_dsn, seed, monkeypatch):
    from streamlit.testing.v1 import AppTest
    for var in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "TOKEN_ENC_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SUPABASE_DB_URL", pg_dsn)
    assert "127.0.0.1" in pg_dsn                                       # local cluster only
    token = "test-token-not-a-secret"
    with conn.cursor() as cur:
        cur.execute("update attendance.managers set access_token_hash = %s, active = true "
                    "where id = %s", (security.hash_token(token), seed["manager_id"]))
    conn.commit()
    run = seed["new_run"]()
    import datetime
    for i, text in enumerate(SENSITIVE):
        seed["new_case"](run, datetime.date(2026, 10, 1 + i), text)

    at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=60)
    at.secrets["SUPABASE_DB_URL"] = pg_dsn
    at.query_params["t"] = token
    at.run()

    assert not at.exception
    shown = "\n".join([m.value for m in at.markdown] + [m.value for m in at.caption]
                      + [m.value for m in at.error] + [m.value for m in at.warning]
                      + [m.value for m in at.info] + [m.value for m in at.success])
    assert "flagged as *Flagged — review required*" in shown
    assert "flagged as *Leave*" in shown
    for word in ("Hospital", "Surgery", "Bereavement", "Annual", "Failed", "Pending",
                 "To Be Confirmed", "check"):
        assert word not in shown
