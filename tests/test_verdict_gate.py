"""The verdict vocabulary and the CRM-proof gate.

A TL may only answer Present or Absent. Marking someone Present asserts they worked, so it
must be backed by a screenshot of that employee's CRM calls for the day; Absent accepts the
original flag and needs nothing. Cases that fail the gate are never written, so they stay
open and the TL can resubmit on the same link.
"""
from app.verdicts import (HRBP_OVERRIDE_VERDICTS, SELECT_PLACEHOLDER, VERDICT_LABEL, VERDICTS,
                          blocked_cases, is_blocked, unanswered, verdict_choices)


def test_tl_can_only_choose_present_or_absent():
    assert VERDICTS == {"Present": "present", "Absent": "absent"}


def test_hrbp_override_adds_leave_codes_after_present_and_absent():
    # HRBP corrects cases where the TL could only say Absent but the day was a real leave.
    assert HRBP_OVERRIDE_VERDICTS == {
        "Present": "present",
        "Absent": "absent",
        "Annual Leave": "annual_leave",
        "Sick Leave": "sick_leave",
        "Unpaid Leave": "unpaid_leave",
        "Other Leave (marriage, bereavement…)": "leave",
    }


def test_hrbp_override_codes_all_render_with_a_label():
    assert all(code in VERDICT_LABEL for code in HRBP_OVERRIDE_VERDICTS.values())


def test_labels_still_resolve_retired_leave_codes():
    # Cases closed before this change keep their stored code; the HRBP view and the
    # reconciled export must render a human label, not the raw enum value.
    assert VERDICT_LABEL["annual_leave"] == "Annual Leave"
    assert VERDICT_LABEL["unpaid_leave"] == "Unpaid Leave"
    assert VERDICT_LABEL["sick_leave"] == "Sick Leave"
    assert VERDICT_LABEL["half_day"] == "Half Day"
    assert VERDICT_LABEL["leave"] == "On Leave"


def test_labels_resolve_the_two_live_codes():
    assert VERDICT_LABEL["present"] == "Present"
    assert VERDICT_LABEL["absent"] == "Absent"


def test_dropdown_leads_with_the_placeholder_so_nothing_is_preselected():
    assert verdict_choices() == [SELECT_PLACEHOLDER, "Present", "Absent"]


def test_placeholder_is_not_an_assignable_verdict():
    assert SELECT_PLACEHOLDER not in VERDICTS


def test_unanswered_case_raises_no_proof_alert():
    # Nothing is chosen yet, so the page must not open covered in red warnings.
    assert is_blocked(None, has_proof=False) is False


def test_unanswered_case_is_never_blocked():
    assert blocked_cases({7: (None, False)}) == []


def test_unanswered_lists_cases_with_no_verdict_chosen():
    selections = {1: ("present", True), 2: (None, False), 3: ("absent", False), 4: (None, True)}
    assert unanswered(selections) == [2, 4]


def test_unanswered_is_empty_when_every_case_is_answered():
    assert unanswered({1: ("present", True), 2: ("absent", False)}) == []


def test_is_blocked_flags_present_without_proof():
    assert is_blocked("present", has_proof=False) is True


def test_is_blocked_clears_present_with_proof():
    assert is_blocked("present", has_proof=True) is False


def test_is_blocked_never_flags_absent():
    assert is_blocked("absent", has_proof=False) is False
    assert is_blocked("absent", has_proof=True) is False


def test_batch_gate_agrees_with_the_single_case_predicate():
    # The TL page warns per case with is_blocked() and rejects per batch with blocked_cases().
    # If these ever diverged, a case could pass the warning and still be refused on submit.
    selections = {1: ("present", True), 2: ("present", False),
                  3: ("absent", False), 4: ("absent", True)}
    assert blocked_cases(selections) == [cid for cid, (code, proof) in selections.items()
                                         if is_blocked(code, proof)]


def test_present_without_proof_is_blocked():
    assert blocked_cases({7: ("present", False)}) == [7]


def test_present_with_proof_is_allowed():
    assert blocked_cases({7: ("present", True)}) == []


def test_absent_needs_no_proof():
    assert blocked_cases({7: ("absent", False)}) == []


def test_absent_may_still_carry_proof():
    assert blocked_cases({7: ("absent", True)}) == []


def test_mixed_batch_blocks_only_unproven_present_cases():
    selections = {
        1: ("present", True),    # proven   -> saves
        2: ("present", False),   # unproven -> blocked
        3: ("absent", False),    # no proof needed -> saves
        4: ("present", False),   # unproven -> blocked
    }
    assert blocked_cases(selections) == [2, 4]


def test_no_selections_blocks_nothing():
    assert blocked_cases({}) == []
