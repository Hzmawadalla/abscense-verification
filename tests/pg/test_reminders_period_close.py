"""Option A (2026-10-16) against real PostgreSQL: cases a period close left open are kept out of
TL reminder counts and recipients, and stay open for the TL link and HRBP.

No test here sends anything. data.managers_overview and app.reminders have no sender; the network
guard fails any non-loopback connection (PostgreSQL itself is reached by libpq on 127.0.0.1)."""
import datetime
import importlib.util
import json
import socket
from pathlib import Path

import pytest

from app import data, reminders
from app.effective import PENDING

_spec = importlib.util.spec_from_file_location(
    "p2_effective", Path(__file__).with_name("test_phase2_effective.py"))
p2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(p2)
org = p2.org                                    # TL-A (E-1, E-2), TL-B (E-3)
_q, _load, _cell, _flag, _case = p2._q, p2._load, p2._cell, p2._flag, p2._case
DAY, LATER = p2.DAY, p2.DAY + datetime.timedelta(days=1)


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    real = socket.socket.connect

    def guarded(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"reminder test tried to reach {host!r}")
        return real(sock, address)
    monkeypatch.setattr(socket.socket, "connect", guarded)


def _overview(conn):
    return {m["crm"]: (m["open_cases"], m["remind_cases"]) for m in data.managers_overview(conn)}


def _notes(conn, crm, day=DAY):
    return p2._audit(conn, _case(conn, crm, day)["id"], data.PERIOD_CLOSE_ACTION)


def test_cases_left_open_by_a_close_are_not_reminded_but_stay_open(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger"), _cell("E-3", DAY, "Absent", "trigger",
                                                               tl="TL-B")],
          [_flag("E-1", DAY), _flag("E-3", DAY, tl="TL-B")])
    assert _overview(conn) == {"TL-A": (1, 1), "TL-B": (1, 1)}
    data.close_open_month(conn, "hrbp:x")
    assert _overview(conn) == {"TL-A": (1, 0), "TL-B": (1, 0)}
    assert reminders.reminder_targets(data.managers_overview(conn))["links"] == []
    for crm in ("E-1", "E-3"):                                  # untouched, still open
        c = _case(conn, crm)
        assert (c["status"], c["final_status"]) == ("open", None)
    tl_a = _q(conn, "select * from attendance.managers where crm = 'TL-A'")[0]
    assert [r["employee_crm"] for r in data.tl_cases_for_manager(conn, tl_a)] == ["E-1"]
    assert [r["effective_status"] for r in data.list_cases(conn)] == [PENDING, PENDING]


def test_a_case_created_after_the_close_is_still_reminded(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger")], [_flag("E-1", DAY)], name="a")
    data.close_open_month(conn, "hrbp:x")
    _load(conn, [_cell("E-2", LATER, "Absent", "trigger")], [_flag("E-2", LATER)], name="b")
    assert _overview(conn)["TL-A"] == (2, 1)
    t = reminders.reminder_targets(data.managers_overview(conn))
    assert [(m["crm"], m["open_cases"]) for m in t["links"]] == [("TL-A", 1)]


def test_counts_and_recipients_agree(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger")], [_flag("E-1", DAY)], name="a")
    data.close_open_month(conn, "hrbp:x")
    _load(conn, [_cell("E-2", LATER, "Absent", "trigger"),
                 _cell("E-3", LATER, "Absent", "trigger", tl="TL-B")],
          [_flag("E-2", LATER), _flag("E-3", LATER, tl="TL-B")], name="b")
    _q(conn, "update attendance.managers set email = lower(crm) || '@test.invalid' "
             "where crm = 'TL-A'")
    overview = data.managers_overview(conn)
    t = reminders.reminder_targets(overview)
    assert {m["crm"]: m["open_cases"] for m in t["links"]} == {"TL-A": 1, "TL-B": 1}
    assert [m["crm"] for m in t["email"]] == ["TL-A"]
    assert [m["crm"] for m in t["no_email"]] == ["TL-B"]
    assert sum(m["open_cases"] for m in t["links"]) == sum(m["remind_cases"] for m in overview)


def test_repeated_closes_stay_deterministic(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger")], [_flag("E-1", DAY)])
    results = [data.close_open_month(conn, "hrbp:x") for _ in range(3)]
    assert results == [{"pending": 1, "kept_for_review": 0}] * 3
    assert len(_notes(conn, "E-1")) == 1
    assert _overview(conn)["TL-A"] == (1, 0)
    # a duplicate note (e.g. written by an older build) changes nothing
    cid = _case(conn, "E-1")["id"]
    _q(conn, "insert into attendance.audit_log (case_id, actor, action) values (%s, 'x', %s)",
       (cid, data.PERIOD_CLOSE_ACTION))
    assert _overview(conn)["TL-A"] == (1, 0)


def test_an_hrbp_void_after_the_close_makes_the_case_remindable_again(conn, org):
    # voiding hands the case back to its TL on purpose ("resend the link"), so it is current again;
    # closing once more puts it back behind the cut-off with a fresh note
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger")], [_flag("E-1", DAY)])
    cid = _case(conn, "E-1")["id"]
    data.close_open_month(conn, "hrbp:x")
    p2._answer(conn, cid, "absent")                            # TL still answers after the close
    assert data.reopen_tl_cases(conn, [cid], "hrbp:x", "resubmit")["reopened"] == 1
    assert _overview(conn)["TL-A"] == (1, 1)
    data.close_open_month(conn, "hrbp:x")
    assert len(_notes(conn, "E-1")) == 2
    assert _overview(conn)["TL-A"] == (1, 0)


def test_only_the_exact_close_note_counts_and_its_contents_are_not_parsed(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger"), _cell("E-2", DAY, "Absent", "trigger")],
          [_flag("E-1", DAY), _flag("E-2", DAY)])
    e1, e2 = _case(conn, "E-1")["id"], _case(conn, "E-2")["id"]
    # other or look-alike actions are not evidence of a close
    for action in ("auto_close_absent", "period_close", "PERIOD_CLOSE_PENDING", "hrbp_close"):
        _q(conn, "insert into attendance.audit_log (case_id, actor, action) values (%s, 'x', %s)",
           (e1, action))
    # the real action with odd contents still is: the action string is the signal
    _q(conn, "insert into attendance.audit_log (case_id, actor, action, new_value) "
             "values (%s, 'x', %s, %s)", (e2, data.PERIOD_CLOSE_ACTION, json.dumps({"x": None})))
    assert _overview(conn)["TL-A"] == (2, 1)
    assert [m["open_cases"] for m in
            reminders.reminder_targets(data.managers_overview(conn))["links"]] == [1]


def test_reminder_filtering_writes_nothing(conn, org):
    _load(conn, [_cell("E-1", DAY, "Absent", "trigger")], [_flag("E-1", DAY)])
    data.close_open_month(conn, "hrbp:x")
    before = _q(conn, "select (select count(*) from attendance.audit_log) as a, "
                      "(select count(*) from attendance.notifications) as n, "
                      "(select string_agg(status::text || coalesce(final_status::text, '-'), ',') "
                      "   from attendance.cases) as s")
    reminders.reminder_targets(data.managers_overview(conn))
    after = _q(conn, "select (select count(*) from attendance.audit_log) as a, "
                     "(select count(*) from attendance.notifications) as n, "
                     "(select string_agg(status::text || coalesce(final_status::text, '-'), ',') "
                     "   from attendance.cases) as s")
    assert before == after and after[0]["n"] == 0
