"""TL-facing status labels: the single, fail-closed privacy policy for everything a Team Leader sees.

A TL only ever sees one of TL_LABELS. The label is derived from the RAW workbook text, never from
classify()'s canonical_status (which can echo free text, e.g. 'Hospital Visit - check' ->
'Hospital Visit'). Rules, in order:

  1. blank / None                                   -> None (no status)
  2. exact match of an approved non-leave status    -> that label   ('absent', 'Absent (HD)')
  3. a recognised leave category or leave code      -> LEAVE        (every leave type, incl.
                                                                     Annual and Unpaid)
  4. flagged but not an exact approved status       -> FLAGGED      ('Absent - To be confirmed')
  5. anything else, including unseen free text      -> UNCLASSIFIED

Only the exact HRBP-facing value carries the original text; it stays in the database untouched.
Kept free of Streamlit so it can be unit-tested (same split as app.verdicts).
"""
from __future__ import annotations

import re

from ingestion.status_rules import SKIP_LEAVES, _base, classify, normalize

LEAVE = "Leave"
FLAGGED = "Flagged — review required"
UNCLASSIFIED = "Unclassified"

# normalized raw value -> approved TL label. Known spelling variants of the same vocabulary entry
# map to one label; nothing outside this table is ever shown verbatim.
_APPROVED = {
    "normal": "Normal", "no leave": "Normal", "present": "Present",
    "weekend": "Weekend", "public holiday": "Public Holiday", "not yet hired": "Not Yet Hired",
    "late": "Late", "missing punch out": "Missing Punch Out",
    "half day": "Half Day", "halfday": "Half Day", "hlaf day": "Half Day",
    "excuse": "Excuse", "2 hour excuse": "Excuse",
    "absent": "Absent", "no show": "No Show",
}

TL_LABELS: frozenset[str] = frozenset({*_APPROVED.values(), LEAVE, FLAGGED, UNCLASSIFIED})

# Leave vocabulary (normalized). Recognising a value as leave only ever produces LEAVE, so this set
# can widen what is generalised but can never expose text.
# Stored verdict codes (cases.manager_status / final_status / leave_type).
_LEAVE_CODES = {"annual_leave", "sick_leave", "unpaid_leave", "leave"}
_LEAVE_WORDS = SKIP_LEAVES | _LEAVE_CODES | {
    "on leave", "leave approved", "leave approval", "sick", "asked for leave, on trip"}
_VERDICT_CODES = {"present": "Present", "absent": "Absent", "half_day": "Half Day"}

# The '(HD)' half-day marker is shown separately (is_half_day), so it never blocks an exact match.
_HD_MARK = re.compile(r"\s*\(hd\)\s*")


def _is_leave(n: str) -> bool:
    return n in _LEAVE_WORDS or _base(n) in _LEAVE_WORDS


def tl_status_label(raw, flagged: bool | None = None) -> str | None:
    """The only status text a TL may see for a workbook value. `flagged` defaults to classify()'s
    verdict; pass True for a case's source_status (a case exists only for a flagged day)."""
    if raw is None:
        return None
    n = normalize(raw)
    if n == "":
        return None
    exact = _HD_MARK.sub(" ", n).strip()
    if exact in _APPROVED:
        return _APPROVED[exact]
    if _is_leave(n):
        return LEAVE
    if flagged is None:
        flagged = classify(n)[0] == "trigger"
    return FLAGGED if flagged else UNCLASSIFIED


def tl_verdict_label(code) -> str | None:
    """A stored verdict code (TL answer or HRBP override) as a TL may see it."""
    if code is None:
        return None
    c = str(code).strip().lower()
    if c in _LEAVE_CODES:
        return LEAVE
    return _VERDICT_CODES.get(c, UNCLASSIFIED)


def tl_case_line(cs) -> str:
    """Markdown header for one open case on the TL page. `cs` is a tl_cases_for_manager row."""
    hd = " · ½ day" if cs["is_half_day"] else ""
    return (f"**{cs['employee_name']}** · `{cs['employee_crm']}` — {cs['work_date']} · "
            f"flagged as *{cs['status_label']}*{hd}")


def tl_done_line(cs) -> str:
    """One locked (already submitted) case on the TL page. The comment is present only when this
    TL wrote it (see data.tl_cases_for_manager)."""
    hd = " · ½ day" if cs["is_half_day"] else ""
    line = (f"**{cs['employee_name']}** · `{cs['employee_crm']}` — {cs['work_date']} · "
            f"flagged *{cs['status_label']}*{hd} → **{cs['verdict_label'] or '—'}**")
    if cs["own_comment"]:
        line += f" · _{cs['own_comment']}_"
    return line
