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


def _render(pg_dsn, token):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=60)
    at.secrets["SUPABASE_DB_URL"] = pg_dsn
    at.query_params["t"] = token
    at.run()
    assert not at.exception, at.exception
    return at


def _all_text(at):
    return "\n".join([m.value for m in at.markdown] + [m.value for m in at.caption]
                     + [m.value for m in at.error] + [m.value for m in at.warning]
                     + [m.value for m in at.info] + [m.value for m in at.success]
                     + [m.value for m in at.title])


def test_expired_invalid_signed_and_legacy_links_on_the_tl_page(conn, pg_dsn, seed, monkeypatch):
    # Phase 3 PR A: an expired or invalid link shows ONE fixed sentence and nothing about the TL,
    # team or cases; a valid signed or legacy link still opens the case page.
    import datetime
    from cryptography.fernet import Fernet
    from app import data, tl_links
    for var in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TOKEN_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SUPABASE_DB_URL", pg_dsn)
    assert "127.0.0.1" in pg_dsn
    seed["new_case"](seed["new_run"](), datetime.date(2026, 10, 1))
    now = datetime.datetime.now(datetime.timezone.utc)

    expired = data.rotate_manager_link(conn, seed["manager_id"], now - datetime.timedelta(days=61),
                                       signed=True)
    at = _render(pg_dsn, expired)
    assert [e.value for e in at.error] == [tl_links.EXPIRED_MESSAGE]
    assert "Lead A" not in _all_text(at) and "Emp One" not in _all_text(at)

    for bad in (expired[:-2] + "xx", "v1.not.a.link", "nope"):
        at = _render(pg_dsn, bad)
        assert [e.value for e in at.error] == [tl_links.INVALID_MESSAGE]
        assert "Lead A" not in _all_text(at)

    signed = data.rotate_manager_link(conn, seed["manager_id"], now, signed=True)
    assert "Lead A" in _all_text(_render(pg_dsn, signed))
    legacy = data.rotate_manager_link(conn, seed["manager_id"], now, signed=False)
    assert "Lead A" in _all_text(_render(pg_dsn, legacy))
    at = _render(pg_dsn, signed)                                      # revoked by the rotation
    assert [e.value for e in at.error] == [tl_links.INVALID_MESSAGE]


def test_team_tab_only_for_a_signed_link_and_shows_the_matrix(conn, pg_dsn, seed, monkeypatch):
    # Phase 3 PR C: a signed link gets the read-only team tab; a legacy link gets the case page only
    import datetime
    from cryptography.fernet import Fernet
    from app import data, tl_matrix
    for var in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TOKEN_ENC_KEY", Fernet.generate_key().decode())
    assert "127.0.0.1" in pg_dsn
    period = tl_matrix.period_options(tl_matrix.business_today(), n=1)[0]
    seed["new_case"](seed["new_run"](), period, "Hospital visit - check")
    now = datetime.datetime.now(datetime.timezone.utc)

    legacy = data.rotate_manager_link(conn, seed["manager_id"], now, signed=False)
    at = _render(pg_dsn, legacy)
    assert len(at.tabs) == 0 and "Lead A" in _all_text(at)

    signed = data.rotate_manager_link(conn, seed["manager_id"], now, signed=True)
    at = _render(pg_dsn, signed)
    assert [t.label for t in at.tabs] == ["✅ Cases to confirm", "📅 Team attendance"]
    table = at.dataframe[0].value
    n_days = (tl_matrix.attendance_period(period)[1] - period).days + 1
    assert list(table["CRM"]) == ["E-1"] and len(table.columns) == 2 + n_days
    day_col = f"{period:%d %b}"
    assert table[day_col][0] == "Pending"                      # flagged-only, undecided
    assert "Absent" not in set(table.iloc[0, 2:])              # missing days are never Absent
    assert "Missing" in set(table.iloc[0, 2:])
    shown = _all_text(at)
    assert "not absence" in shown                                # coverage warning is shown
    for word in ("Hospital", "check"):
        assert word not in shown and word not in table.to_string()
