"""Effective status (Phase 2): HRBP Override > TL Response > Original Attendance.

One pure function decides it for a CRM x date, from the ingredients Phase 1 keeps separate
(app.attendance_view grid rows, or a case row joined to its active source cell). Nothing here
writes; the ingredients stay stored apart, so every layer remains visible to HRBP and the audit.

Rules (in order):
  1. an HRBP Override (hrbp_override_at set)          -> its value          source "hrbp_override"
  2. a TL answer (manager_status)                      -> the TL's verdict   source "tl"
  3. an undecided case: open, or closed only by the   -> PENDING while the active cell is still
     old period close (closed_by = 'hrbp_cutoff', no      flagged (or no cell covers the day), else
     TL answer, no override): that Absent was a default,  REVIEW_REQUIRED: a newer upload no longer
     never a decision. The stored value is kept as is.    flags it, and nobody has decided yet
  4. a case closed with a final status but no answer   -> that status        source = closed_by
  5. no deciding case                                  -> the active source cell (BLANK when the
                                                          newest upload left it empty), else MISSING

Missing attendance is never Absent: MISSING, BLANK, PENDING and REVIEW_REQUIRED are explicit
states, never a verdict.

HRBP-facing only. The TL page keeps its own fail-closed labels (app.tl_labels).
"""
from __future__ import annotations

from typing import NamedTuple

from app.verdicts import HRBP_OVERRIDE_VERDICTS, VERDICT_LABEL

PENDING = "Pending"
REVIEW_REQUIRED = "Review required"
MISSING = "Missing"
BLANK = "Blank"
UNRESOLVED_STATES = frozenset({PENDING, REVIEW_REQUIRED})

SOURCE_OVERRIDE = "hrbp_override"
SOURCE_TL = "tl"
SOURCE_CASE = "case"
SOURCE_ORIGINAL = "original"
SOURCE_NONE = "none"

# closed_by of the pre-2026-10 period close, which stored final_status = 'absent' with no decision.
# app.data.CASE_UNDECIDED is the SQL twin of is_undecided(); tests keep the two in step.
CLOSED_BY_CUTOFF = "hrbp_cutoff"
OPEN_STATES = ("open", "manager_responded")

# The only codes an HRBP override may store (server-side check, not just the dropdown).
OVERRIDE_CODES: frozenset[str] = frozenset(HRBP_OVERRIDE_VERDICTS.values())


class Effective(NamedTuple):
    status: str          # display label: a verdict label, the source cell's status, or a state
    source: str          # which layer decided it (SOURCE_* or a case's closed_by)
    needs_review: bool   # the source moved after a decision, or no longer flags an open case


def validate_override(code) -> str:
    """Return `code` when HRBP may store it as an override; raise ValueError otherwise."""
    if code not in OVERRIDE_CODES:
        raise ValueError(f"unsupported HRBP override value: {code!r}")
    return code


def is_undecided(case_status, closed_by, tl_answer, hrbp_override_at) -> bool:
    """A case nobody has decided: still open, or closed only by the old period-close default."""
    if hrbp_override_at is not None or tl_answer:
        return False
    return case_status in OPEN_STATES or closed_by == CLOSED_BY_CUTOFF


def _label(code) -> str:
    return VERDICT_LABEL.get(code, code)


def effective_status(*, case_status=None, tl_answer=None, final_status=None, closed_by=None,
                     hrbp_override_at=None, active_bucket=None, active_status=None,
                     active_raw=None, source_changed_since_response=False) -> Effective:
    """Decide one CRM x date. `case_status` is None when no case exists; `active_bucket` is None
    when no upload covers the day ('blank' when the newest upload left it empty)."""
    has_case = case_status is not None
    flagged_now = active_bucket is None or active_bucket == "trigger"
    undecided = has_case and is_undecided(case_status, closed_by, tl_answer, hrbp_override_at)
    unflagged_undecided = undecided and not flagged_now
    review = bool(source_changed_since_response) or unflagged_undecided

    if has_case and hrbp_override_at is not None and final_status:
        return Effective(_label(final_status), SOURCE_OVERRIDE, review)
    if has_case and tl_answer:
        return Effective(_label(tl_answer), SOURCE_TL, review)
    if undecided:
        return Effective(REVIEW_REQUIRED if unflagged_undecided else PENDING, SOURCE_CASE, review)
    if has_case and final_status:
        return Effective(_label(final_status), closed_by or SOURCE_CASE, review)
    if active_bucket == "blank":
        return Effective(BLANK, SOURCE_ORIGINAL, review)
    if active_bucket is not None:
        return Effective(active_status or active_raw or MISSING, SOURCE_ORIGINAL, review)
    if has_case:                      # closed without any decision recorded: nothing to stand on
        return Effective(PENDING, SOURCE_CASE, review)
    return Effective(MISSING, SOURCE_NONE, review)


def for_grid_row(r) -> Effective:
    """Effective status of one app.attendance_view grid row (after _derive)."""
    return effective_status(
        case_status=r.get("case_status"), tl_answer=r.get("tl_answer"),
        final_status=r.get("final_status"), closed_by=r.get("closed_by"),
        hrbp_override_at=r.get("hrbp_override_at"), active_bucket=r.get("bucket"),
        active_status=r.get("canonical_status"), active_raw=r.get("raw_value"),
        source_changed_since_response=r.get("source_changed_since_response", False))


def for_case_row(r) -> Effective:
    """Effective status of one data.list_cases row (carries active_bucket / active_raw)."""
    changed = bool(r.get("manager_responded_at") and r.get("active_bucket") is not None
                   and r.get("active_raw") != r.get("source_status"))
    return effective_status(
        case_status=r.get("status"), tl_answer=r.get("manager_status"),
        final_status=r.get("final_status"), closed_by=r.get("closed_by"),
        hrbp_override_at=r.get("hrbp_override_at"), active_bucket=r.get("active_bucket"),
        active_status=r.get("active_status"), active_raw=r.get("active_raw"),
        source_changed_since_response=changed)
