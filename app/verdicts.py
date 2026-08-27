"""Verdict vocabulary and the CRM-proof gate.

Kept free of any Streamlit dependency so the gate can be unit-tested without booting the app
(same split as `app.security` / `app.report`).

A TL answers Present or Absent, nothing else. Marking someone Present asserts they worked that
day, so it must be backed by a screenshot of their CRM calls; Absent accepts the flag the
attendance report already raised and needs no evidence.
"""
from __future__ import annotations

from uuid import UUID

# Verdict dropdown (label -> stored enum code), shared by the TL page and the HRBP override.
VERDICTS: dict[str, str] = {
    "Present": "present",
    "Absent": "absent",
}

# Display map (code -> label) for the HRBP view and the reconciled export. Deliberately NOT
# derived from VERDICTS: the Postgres enum still holds the retired leave codes (Postgres cannot
# cleanly drop an enum value), and cases closed before the binary change keep them. Dropping
# these entries would render historical rows as raw codes like 'annual_leave'.
VERDICT_LABEL: dict[str, str] = {
    "present": "Present",
    "absent": "Absent",
    # retired — never assignable again, still readable on historical rows
    "annual_leave": "Annual Leave",
    "unpaid_leave": "Unpaid Leave",
    "sick_leave": "Sick Leave",
    "half_day": "Half Day",
    "leave": "On Leave",
}

# Verdict codes that a TL must evidence with a CRM-calls screenshot.
PROOF_REQUIRED: frozenset[str] = frozenset({"present"})

# Leading dropdown entry, so no verdict is pre-selected. Without it the first real option would
# be chosen on load — the TL could submit an answer they never consciously made, and every case
# would open showing the "attach a screenshot" alert. Maps to a None code, never to an enum value.
SELECT_PLACEHOLDER: str = "Select…"


def verdict_choices() -> list[str]:
    """Dropdown options for the TL page: the placeholder first, then the assignable verdicts."""
    return [SELECT_PLACEHOLDER, *VERDICTS]


def unanswered(selections: dict[UUID, tuple[str | None, bool]]) -> list[UUID]:
    """Case ids still sitting on the placeholder. Same `selections` shape as `blocked_cases`.

    These are not rejections — the TL simply hasn't answered yet — so they are skipped on
    submit and reported separately from the cases refused for missing proof.
    """
    return [cid for cid, (code, _has_proof) in selections.items() if code is None]


def is_blocked(code: str | None, has_proof: bool) -> bool:
    """Whether one answer asserts attendance without evidence, and so must not be written.

    Single source of truth for the gate: the TL page warns with this while the form is being
    filled in, and rejects with `blocked_cases` on submit. Sharing the rule keeps the warning
    and the rejection from ever disagreeing.
    """
    return code in PROOF_REQUIRED and not has_proof


def blocked_cases(selections: dict[UUID, tuple[str | None, bool]]) -> list[UUID]:
    """Case ids that must NOT be written because they assert attendance without evidence.

    `selections` maps case id -> (verdict code or None if unanswered, whether a usable
    screenshot is attached). Unanswered cases are not blocked — see `unanswered`.
    Returns the blocked ids in selection order; the caller submits everything else and leaves
    these cases open so the TL can attach proof and resubmit.
    """
    return [cid for cid, (code, has_proof) in selections.items() if is_blocked(code, has_proof)]
