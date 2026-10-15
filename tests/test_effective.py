"""Phase 2 effective status: HRBP Override > TL Response > Original Attendance (app.effective)."""
import datetime

import pytest

from app import effective as eff
from app.effective import (BLANK, MISSING, PENDING, REVIEW_REQUIRED, SOURCE_CASE, SOURCE_NONE,
                           SOURCE_ORIGINAL, SOURCE_OVERRIDE, SOURCE_TL, effective_status)
from app.verdicts import HRBP_OVERRIDE_VERDICTS, VERDICTS

AT = datetime.datetime(2026, 10, 1, 9, 0)


# ------------------------------------------------------------------ precedence
def test_override_beats_tl_and_original():
    e = effective_status(case_status="closed", tl_answer="present", final_status="sick_leave",
                         closed_by="hrbp", hrbp_override_at=AT, active_bucket="trigger",
                         active_status="Absent")
    assert (e.status, e.source) == ("Sick Leave", SOURCE_OVERRIDE)


def test_override_decides_a_case_the_tl_never_answered():
    e = effective_status(case_status="closed", final_status="present", closed_by="hrbp",
                         hrbp_override_at=AT, active_bucket="trigger", active_status="Absent")
    assert (e.status, e.source) == ("Present", SOURCE_OVERRIDE)


@pytest.mark.parametrize("answer,label", [("present", "Present"), ("absent", "Absent")])
def test_tl_answer_beats_original_without_override(answer, label):
    e = effective_status(case_status="closed", tl_answer=answer, final_status=answer,
                         closed_by="tl", active_bucket="trigger", active_status="Absent")
    assert (e.status, e.source) == (label, SOURCE_TL)


def test_hrbp_accepting_the_tl_answer_is_still_the_tl_answer():
    # close_case without final_status = accept the TL verdict: no hrbp_override_at is set
    e = effective_status(case_status="closed", tl_answer="present", final_status="present",
                         closed_by="hrbp", active_bucket="trigger")
    assert (e.status, e.source) == ("Present", SOURCE_TL)


def test_a_final_status_without_override_timestamp_is_not_an_override():
    # final_status alone (e.g. the period close) must never be read as an HRBP override
    e = effective_status(case_status="closed", final_status="absent", closed_by="hrbp_cutoff",
                         active_bucket="trigger")
    assert (e.status, e.source) == ("Absent", "hrbp_cutoff")


# ------------------------------------------------------------------ unresolved
def test_open_flagged_case_is_pending_not_absent():
    e = effective_status(case_status="open", active_bucket="trigger", active_status="Absent")
    assert (e.status, e.source, e.needs_review) == (PENDING, SOURCE_CASE, False)


def test_open_case_without_any_stored_cell_is_pending():
    # legacy uploads (before Phase 1) kept only the case, not the cell
    assert effective_status(case_status="open").status == PENDING


@pytest.mark.parametrize("bucket", ["skip", "blank", "not_verified", "ignore"])
def test_open_case_no_longer_flagged_is_review_required(bucket):
    e = effective_status(case_status="open", active_bucket=bucket, active_status="Normal")
    assert (e.status, e.source, e.needs_review) == (REVIEW_REQUIRED, SOURCE_CASE, True)


def test_answered_case_keeps_the_answer_when_the_source_changes_but_needs_review():
    e = effective_status(case_status="closed", tl_answer="absent", final_status="absent",
                         closed_by="tl", active_bucket="skip", active_status="Normal",
                         source_changed_since_response=True)
    assert (e.status, e.source, e.needs_review) == ("Absent", SOURCE_TL, True)


def test_closed_case_with_no_decision_at_all_stays_pending():
    assert effective_status(case_status="closed", closed_by="hrbp").status == PENDING


# ------------------------------------------------------------------ original / missing
def test_no_case_uses_the_active_source_cell():
    e = effective_status(active_bucket="skip", active_status="Annual Leave", active_raw="AL")
    assert (e.status, e.source) == ("Annual Leave", SOURCE_ORIGINAL)


def test_original_falls_back_to_the_raw_text_when_not_classified():
    assert effective_status(active_bucket="ignore", active_raw="#N/A").status == "#N/A"


def test_blank_newest_cell_is_blank_not_absent():
    e = effective_status(active_bucket="blank")
    assert (e.status, e.source) == (BLANK, SOURCE_ORIGINAL)


def test_no_cell_and_no_case_is_missing_not_absent():
    e = effective_status()
    assert (e.status, e.source, e.needs_review) == (MISSING, SOURCE_NONE, False)


def test_unresolved_states_are_never_verdict_labels():
    states = {PENDING, REVIEW_REQUIRED, MISSING, BLANK}
    assert states.isdisjoint(set(HRBP_OVERRIDE_VERDICTS)) and "Absent" not in states


# ------------------------------------------------------------------ override vocabulary
def test_override_codes_are_the_hrbp_dropdown_codes():
    assert eff.OVERRIDE_CODES == frozenset(HRBP_OVERRIDE_VERDICTS.values())
    assert set(VERDICTS.values()) <= eff.OVERRIDE_CODES


@pytest.mark.parametrize("code", sorted(HRBP_OVERRIDE_VERDICTS.values()))
def test_supported_override_codes_validate(code):
    assert eff.validate_override(code) == code


@pytest.mark.parametrize("code", ["half_day", "Present", "excused", "", None, "absent; drop"])
def test_unsupported_override_codes_are_rejected(code):
    # half_day is a retired enum value: readable on history, never assignable
    with pytest.raises(ValueError):
        eff.validate_override(code)


# ------------------------------------------------------------------ row adapters
def _case_row(**kw):
    base = {"status": "closed", "manager_status": "absent", "final_status": "absent",
            "closed_by": "tl", "hrbp_override_at": None, "source_status": "Absent",
            "manager_responded_at": AT, "active_bucket": "trigger", "active_raw": "Absent",
            "active_status": "Absent"}
    return {**base, **kw}


def test_case_row_unchanged_source_needs_no_review():
    assert eff.for_case_row(_case_row()).needs_review is False


def test_case_row_source_changed_after_the_answer_needs_review():
    e = eff.for_case_row(_case_row(active_bucket="skip", active_raw="Normal",
                                   active_status="Normal"))
    assert (e.status, e.needs_review) == ("Absent", True)


def test_case_row_without_any_stored_cell_is_not_flagged_as_changed():
    e = eff.for_case_row(_case_row(active_bucket=None, active_raw=None, active_status=None))
    assert e.needs_review is False


def test_grid_row_adapter_uses_the_grid_column_names():
    r = {"case_status": "open", "tl_answer": None, "final_status": None, "closed_by": None,
         "hrbp_override_at": None, "bucket": "blank", "canonical_status": None,
         "raw_value": None, "source_changed_since_response": False}
    assert eff.for_grid_row(r).status == REVIEW_REQUIRED
