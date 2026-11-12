"""Waive screen (Period close tab) through Streamlit AppTest against a throwaway local PostgreSQL.
Same safety rule and sign-in stub as test_hrbp_page_render (its helpers are reused)."""
import datetime
import importlib.util
from pathlib import Path

import pytest

from ingestion.summary import CaseCandidate

_spec = importlib.util.spec_from_file_location(
    "hrbp_render", Path(__file__).with_name("test_hrbp_page_render.py"))
r = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(r)
pytestmark = r.pytestmark
D = datetime.date
DAY1, DAY2 = D(2026, 9, 20), D(2026, 9, 21)


def _two_cases(conn):
    r._load(conn, [r._cell("E-1", DAY1, "Absent", "trigger"), r._cell("E-1", DAY2, "Absent", "trigger")],
            [CaseCandidate("E-1", "TL-A", DAY1, "Absent"), CaseCandidate("E-1", "TL-A", DAY2, "Absent")])


def _button(at, prefix):
    return next(b for b in at.button if b.label.startswith(prefix))


def _cases(conn):
    with conn.cursor() as cur:
        cur.execute("select work_date, status, final_status, closed_by from attendance.cases "
                    "order by work_date")
        return cur.fetchall()


def _ready(conn, pg_dsn, monkeypatch):
    _two_cases(conn)
    at = r._signed_in_app(conn, pg_dsn, monkeypatch)
    at.run()
    assert not at.exception, at.exception
    assert "Waive unanswered cases" in r._text(at)
    assert "2 of 2 unanswered case(s) shown." in r._text(at)
    return at


def test_waive_needs_a_reason_and_a_confirmation(conn, pg_dsn, seed, monkeypatch):
    at = _ready(conn, pg_dsn, monkeypatch)
    assert _button(at, "Waive all shown (2)").disabled                    # no reason yet
    assert _button(at, "Waive selected (0)").disabled
    at.date_input(key="wv_from").set_value(DAY2)
    at.run()
    assert "1 of 2 unanswered case(s) shown." in r._text(at)
    at.text_input(key="wv_reason").set_value("TL on leave")
    at.run()
    _button(at, "Waive all shown (1)").click()
    at.run()
    assert not at.exception, at.exception
    shown = r._text(at)
    assert "waive **1** case(s) as **Absent** for **1** employee(s)" in shown
    assert [c[1] for c in _cases(conn)] == ["open", "open"]              # nothing written yet
    _button(at, "Confirm waive").click()
    at.run()
    assert not at.exception, at.exception
    assert "Waived 1 case(s) as Absent." in r._text(at)
    assert _cases(conn) == [(DAY1, "open", None, None), (DAY2, "closed", "absent", "hrbp_waived")]
    assert "0 of 1 unanswered case(s) shown." in r._text(at)       # From-date filter still on


def test_cancel_writes_nothing(conn, pg_dsn, seed, monkeypatch):
    at = _ready(conn, pg_dsn, monkeypatch)
    at.text_input(key="wv_reason").set_value("bulk")
    at.run()
    _button(at, "Waive all shown (2)").click()
    at.run()
    _button(at, "Cancel").click()
    at.run()
    assert not at.exception, at.exception
    assert [c[1] for c in _cases(conn)] == ["open", "open"]
    assert "2 of 2 unanswered case(s) shown." in r._text(at)


def test_an_answered_case_is_not_offered(conn, pg_dsn, seed, monkeypatch):
    _two_cases(conn)
    from app import data
    with conn.cursor() as cur:
        cur.execute("select id from attendance.cases where work_date = %s", (DAY1,))
        cid = cur.fetchone()[0]
    assert data.submit_verdict(conn, cid, "present", None, "was in", "tl:TL-A")
    at = r._signed_in_app(conn, pg_dsn, monkeypatch)
    at.run()
    assert not at.exception, at.exception
    assert "1 of 1 unanswered case(s) shown." in r._text(at)
