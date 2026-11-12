"""Waive (HRBP decision 2026-11-12): close cases no TL has answered yet as Absent, either every
case the filters show or a ticked selection.

A waive is an HRBP decision, not a default: the case closes with final_status 'absent' and
closed_by 'hrbp_waived', so app.effective reads it Absent (rule 4, "decided by hrbp_waived") and
the export writes Absent. Cases a TL already answered are never waivable — they stay in the normal
HRBP review. The reason and the actor live in the case's audit entry ('hrbp_waive').

Pure helpers only (no database, no Streamlit): filtering, the confirmation summary, input checks,
and the two-step confirm state kept in a caller-supplied dict (st.session_state in the app).
"""
from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable, Mapping, MutableMapping, Sequence

WAIVE_CLOSED_BY = "hrbp_waived"
WAIVE_ACTION = "hrbp_waive"
WAIVE_VERDICT = "absent"
MAX_REASON = 500
PENDING_KEY = "waive_pending"
NO_TL = "(no team leader)"     # filter value for cases without a team leader (manager_id NULL)

_INVISIBLE = dict.fromkeys(map(ord, "​﻿ "), " ")


def validate_reason(reason) -> str:
    """The trimmed reason; ValueError when blank (invisible characters count as blank) or too long."""
    text = reason.translate(_INVISIBLE).strip() if isinstance(reason, str) else ""
    if not text:
        raise ValueError("a reason is required to waive cases")
    if len(text) > MAX_REASON:
        raise ValueError(f"the reason is longer than {MAX_REASON} characters")
    return text


def normalize_ids(case_ids: Iterable) -> tuple[uuid.UUID, ...]:
    """Case ids (UUIDs, or their text form) as a de-duplicated tuple in their original order;
    ValueError on anything else."""
    seen: dict[uuid.UUID, None] = {}
    for cid in case_ids:
        if isinstance(cid, uuid.UUID):
            key = cid
        elif isinstance(cid, str):
            try:
                key = uuid.UUID(cid.strip())
            except ValueError:
                raise ValueError(f"invalid case id: {cid!r}") from None
        else:
            raise ValueError(f"invalid case id: {cid!r}")
        seen.setdefault(key, None)
    return tuple(seen)


def _crm_key(crm) -> str:
    return (crm or "").translate(_INVISIBLE).strip().lower()


def filter_cases(rows: Sequence[Mapping], *, employee_crm: str | None = None,
                 manager_id=None, date_from: dt.date | None = None,
                 date_to: dt.date | None = None) -> list[dict]:
    """The rows matching every filter given (None = no filter; manager_id=NO_TL = cases without a
    team leader). Employees match on CRM, never name. Returns new dicts; the input is unchanged."""
    want_crm = _crm_key(employee_crm) if employee_crm else None
    want_tl = None if manager_id == NO_TL else manager_id
    return [dict(r) for r in rows
            if (want_crm is None or _crm_key(r["employee_crm"]) == want_crm)
            and (manager_id is None or r["manager_id"] == want_tl)
            and (date_from is None or r["work_date"] >= date_from)
            and (date_to is None or r["work_date"] <= date_to)]


def summary(rows: Sequence[Mapping]) -> dict:
    """{'cases', 'employees' (distinct CRMs), 'review' (no longer flagged by the newest upload)}
    for the confirmation message."""
    return {"cases": len(rows), "employees": len({_crm_key(r["employee_crm"]) for r in rows}),
            "review": sum(1 for r in rows if r.get("unflagged"))}


def stage(state: MutableMapping, rows: Sequence[Mapping], reason) -> dict:
    """Hold a waive for confirmation. Nothing is written until the caller confirms with take()."""
    if not rows:
        raise ValueError("no case selected")
    staged = {"ids": normalize_ids(r["id"] for r in rows), "reason": validate_reason(reason),
              **summary(rows)}
    state[PENDING_KEY] = staged
    return staged


def pending(state: Mapping) -> dict | None:
    return state.get(PENDING_KEY)


def take(state: MutableMapping) -> dict | None:
    """The staged waive, removed from the state (so a rerun can't apply it twice)."""
    return state.pop(PENDING_KEY, None)


def cancel(state: MutableMapping) -> None:
    state.pop(PENDING_KEY, None)
