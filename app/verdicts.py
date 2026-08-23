"""Verdict vocabulary and the CRM-proof gate.

Kept free of any Streamlit dependency so the gate can be unit-tested without booting the app
(same split as `app.security` / `app.report`).

A TL answers Present or Absent, nothing else. Marking someone Present asserts they worked that
day, so it must be backed by a screenshot of their CRM calls; Absent accepts the flag the
attendance report already raised and needs no evidence.
"""
from __future__ import annotations

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


def blocked_cases(selections: dict[int, tuple[str, bool]]) -> list[int]:
    """Case ids that must NOT be written because they assert attendance without evidence.

    `selections` maps case id -> (verdict code, whether a usable screenshot is attached).
    Returns the blocked ids in selection order; the caller submits everything else and leaves
    these cases open so the TL can attach proof and resubmit.
    """
    return [cid for cid, (code, has_proof) in selections.items()
            if code in PROOF_REQUIRED and not has_proof]
