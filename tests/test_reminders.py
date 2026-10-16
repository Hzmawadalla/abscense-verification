"""app.reminders: who a TL reminder goes to and with what count (option A, 2026-10-16). Pure —
nothing here can send: the module has no sender, and the network guard below fails any attempt."""
import socket

import pytest

from app import reminders as r


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **kw):
        raise AssertionError("a reminder test tried to open a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def _tl(crm, open_cases, remind_cases, email=None, dingtalk=None):
    return {"id": crm, "crm": crm, "name": crm, "email": email, "dingtalk_userid": dingtalk,
            "open_cases": open_cases, "remind_cases": remind_cases}


OVERVIEW = [
    _tl("A", 3, 1, email="a@test.invalid", dingtalk="dA"),   # 2 of 3 left open by a close
    _tl("B", 2, 0, email="b@test.invalid", dingtalk="dB"),   # all left open by a close
    _tl("C", 1, 1),                                           # no email, no DingTalk
    _tl("D", 0, 0, email="d@test.invalid"),                   # nothing open
    _tl("E", 4, 4, dingtalk="dE"),                            # DingTalk only
]


def test_only_tls_with_cases_to_remind_are_targets():
    t = r.reminder_targets(OVERVIEW)
    assert [m["crm"] for m in t["links"]] == ["A", "C", "E"]
    assert [m["crm"] for m in t["email"]] == ["A"]
    assert [m["crm"] for m in t["no_email"]] == ["C", "E"]
    assert [m["crm"] for m in t["dingtalk"]] == ["A", "E"]


def test_target_rows_carry_the_reminder_count_not_the_open_count():
    t = r.reminder_targets(OVERVIEW)
    assert {m["crm"]: m["open_cases"] for m in t["links"]} == {"A": 1, "C": 1, "E": 4}


def test_counts_and_recipients_agree():
    t = r.reminder_targets(OVERVIEW)
    assert sum(m["open_cases"] for m in t["links"]) == sum(r.remind_count(m) for m in OVERVIEW)
    assert len(t["email"]) + len(t["no_email"]) == len(t["links"])
    assert all(r.remind_count(m) > 0 for k in t for m in t[k])


def test_the_overview_rows_are_not_changed():
    before = [dict(m) for m in OVERVIEW]
    r.reminder_targets(OVERVIEW)
    r.with_remind_count(OVERVIEW[0])
    assert OVERVIEW == before


@pytest.mark.parametrize("value", [None, "3", -1, 2.5])
def test_a_missing_or_malformed_count_reminds_nobody(value):
    row = {**_tl("X", 3, 0, email="x@test.invalid"), "remind_cases": value}
    assert r.remind_count(row) == 0
    assert r.reminder_targets([row])["links"] == []
    no_field = {k: v for k, v in row.items() if k != "remind_cases"}
    assert r.remind_count(no_field) == 0


def test_with_remind_count_keeps_everything_else():
    m = r.with_remind_count(OVERVIEW[0])
    assert m == {**OVERVIEW[0], "open_cases": 1}


def test_with_remind_count_is_idempotent():
    # the senders re-apply it to whatever row they get, so applying it twice must change nothing
    once = r.with_remind_count(OVERVIEW[0])
    assert r.with_remind_count(once) == once
