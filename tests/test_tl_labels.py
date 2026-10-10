"""Fail-closed TL label policy: every output is an approved label, no input text ever leaks."""
import datetime

import pytest

from app.tl_labels import (FLAGGED, LEAVE, TL_LABELS, UNCLASSIFIED, tl_case_line, tl_done_line,
                           tl_status_label, tl_verdict_label)

# Synthetic sensitive inputs (no real data). Each must reach the TL as a label only.
SENSITIVE = [
    ("Hospital visit - check", FLAGGED),
    ("Surgery (Pending)", FLAGGED),
    ("Doctor appointment - deducted from balance", UNCLASSIFIED),
    ("Bereavement Leave - To Be Confirmed", LEAVE),
    ("Marriage Leave", LEAVE),
    ("Paternity Leave (Returned)", LEAVE),
    ("Sick Leave - had surgery", LEAVE),
    ("Unpaid Leave (HD) - To Be Confirmed", LEAVE),
    ("Weird free text about a family matter", UNCLASSIFIED),
    ("Absent - To be confirmed", FLAGGED),
    ("Absent - see medical note", FLAGGED),
    ("Normal but was at the clinic", UNCLASSIFIED),
    ("Late (doctor)", UNCLASSIFIED),
]


@pytest.mark.parametrize("raw, label", [
    # rule 1: nothing
    (None, None), ("", None), ("   ", None),
    # rule 2: exact approved statuses — case and whitespace insensitive, (HD) marker tolerated
    ("Normal", "Normal"), ("  normal  ", "Normal"), ("NO SHOW", "No Show"), ("Absent", "Absent"),
    ("Absent (HD)", "Absent"), ("absent(hd)", "Absent"), ("Late", "Late"), ("Weekend", "Weekend"),
    ("Public Holiday", "Public Holiday"), ("Not Yet Hired", "Not Yet Hired"), ("Present", "Present"),
    ("Half Day", "Half Day"), ("halfday", "Half Day"), ("Missing Punch Out", "Missing Punch Out"),
    ("2 Hour Excuse", "Excuse"), ("No Leave", "Normal"),
    # rule 3: every leave category, Annual and Unpaid included, plus codes
    ("Annual Leave", LEAVE), ("Unpaid Leave", LEAVE), ("SICK LEAVE", LEAVE), ("Sick", LEAVE),
    ("Casual Leave", LEAVE), ("Continuing Education Leave", LEAVE), ("Leave Approved", LEAVE),
    ("Annual Leave (Failed)", LEAVE), ("annual_leave", LEAVE), ("sick_leave", LEAVE),
    ("Asked for leave, on trip", LEAVE),
    # rules 4/5: anything else, by flagged-ness
    *SENSITIVE,
])
def test_status_label_table(raw, label):
    assert tl_status_label(raw) == label


@pytest.mark.parametrize("raw, _label", SENSITIVE)
def test_no_input_word_reaches_the_tl(raw, _label):
    out = tl_status_label(raw)
    assert out in TL_LABELS
    for word in ("hospital", "surgery", "doctor", "medical", "clinic", "family", "check",
                 "pending", "confirmed", "deducted", "balance", "bereavement", "marriage",
                 "paternity", "sick", "unpaid"):
        if word in raw.lower():
            assert word not in out.lower()


def test_case_source_is_flagged_even_when_classify_would_not_say_so():
    assert tl_status_label("Some new wording", flagged=True) == FLAGGED
    assert tl_status_label("Some new wording", flagged=False) == UNCLASSIFIED
    assert tl_status_label("Absent", flagged=True) == "Absent"


@pytest.mark.parametrize("seed", range(200))
def test_unseen_input_always_lands_on_an_approved_label(seed):
    import random
    rnd = random.Random(seed)
    alphabet = "abcdefghijklmnopqrstuvwxyz ()-_,.0123456789ABCDEF"
    raw = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 40)))
    out = tl_status_label(raw)
    assert out is None or out in TL_LABELS


@pytest.mark.parametrize("code, label", [
    ("present", "Present"), ("absent", "Absent"), ("half_day", "Half Day"),
    ("annual_leave", LEAVE), ("unpaid_leave", LEAVE), ("sick_leave", LEAVE), ("leave", LEAVE),
    ("Annual_Leave", LEAVE), (None, None), ("something_new", UNCLASSIFIED),
])
def test_verdict_label(code, label):
    assert tl_verdict_label(code) == label


def _row(**kw):
    base = {"employee_name": "Emp One", "employee_crm": "E-1",
            "work_date": datetime.date(2026, 10, 1), "is_half_day": False,
            "status_label": FLAGGED, "verdict_label": "Absent", "own_comment": None}
    return {**base, **kw}


def test_case_lines_render_labels_only():
    assert tl_case_line(_row()) == ("**Emp One** · `E-1` — 2026-10-01 · "
                                    "flagged as *Flagged — review required*")
    assert tl_case_line(_row(is_half_day=True)).endswith("· ½ day")
    assert tl_done_line(_row()).endswith("→ **Absent**")
    assert tl_done_line(_row(own_comment="was sick")).endswith(" · _was sick_")
    assert tl_done_line(_row(verdict_label=None)).endswith("→ **—**")
