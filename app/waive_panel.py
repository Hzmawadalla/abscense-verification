"""HRBP screen for waiving unanswered cases (app.waive). Kept out of streamlit_app.py so the main
file only gains a call. Flow: filter -> tick or take all shown -> type a reason -> confirm -> write."""
from __future__ import annotations

import hashlib

import pandas as pd
import streamlit as st

from app import data, waive

_ALL = "(all)"


def _fingerprint(rows) -> str:
    return hashlib.sha256("|".join(str(r["id"]) for r in rows).encode()).hexdigest()[:16]


def _employee_options(rows) -> tuple[list, dict]:
    people = {r["employee_crm"]: r["employee_name"] for r in rows}
    return [None, *sorted(people, key=lambda crm: ((people[crm] or "").lower(), crm))], people


def _filters(rows) -> list[dict]:
    """The filter row; returns the matching cases (new dicts)."""
    crms, people = _employee_options(rows)
    # A case without a team leader gets the NO_TL option, never None (None means "all").
    tls = {(r["manager_id"] if r["manager_id"] is not None else waive.NO_TL):
           (r["manager_name"] or waive.NO_TL) for r in rows}
    f = st.columns(4)
    crm = f[0].selectbox("Employee", crms, key="wv_emp",
                         format_func=lambda c: _ALL if c is None else f"{people[c]} · {c}")
    tl = f[1].selectbox("Team leader", [None, *sorted(tls, key=lambda m: tls[m].lower())],
                        key="wv_tl", format_func=lambda m: _ALL if m is None else tls[m])
    d_from = f[2].date_input("From date", value=None, key="wv_from")
    d_to = f[3].date_input("To date", value=None, key="wv_to")
    return waive.filter_cases(rows, employee_crm=crm, manager_id=tl, date_from=d_from, date_to=d_to)


def _table(shown) -> list[dict]:
    """The tickable list; returns the ticked cases."""
    df = pd.DataFrame([{"waive": False, "employee": r["employee_name"], "crm": r["employee_crm"],
                        "team_leader": r["manager_name"], "date": r["work_date"],
                        "flagged_as": r["source_status"] + (" · ½ day" if r["is_half_day"] else ""),
                        "review": bool(r.get("unflagged")),
                        "id": str(r["id"])} for r in shown])
    edited = st.data_editor(
        # Ticks are stored by row position, so the key follows the exact list shown: a filter
        # change starts a fresh table and a tick can never land on a different case.
        df, hide_index=True, width="stretch", key=f"wv_table_{_fingerprint(shown)}",
        disabled=["employee", "crm", "team_leader", "date", "flagged_as", "review", "id"],
        column_config={"waive": st.column_config.CheckboxColumn("Waive", default=False),
                       "crm": st.column_config.TextColumn("CRM"),
                       "team_leader": st.column_config.TextColumn("Team leader"),
                       "flagged_as": st.column_config.TextColumn("Flagged as"),
                       "review": st.column_config.CheckboxColumn(
                           "⚠️ Review required",
                           help="The newest upload no longer flags this day."),
                       "id": None})
    ticked = set(edited.loc[edited["waive"], "id"])
    return [r for r in shown if str(r["id"]) in ticked]


def _confirm(c, actor, staged) -> None:
    st.warning(f"You're about to waive **{staged['cases']}** case(s) as **Absent** for "
               f"**{staged['employees']}** employee(s).\n\nReason: *{staged['reason']}*\n\n"
               "Team leaders will no longer be able to answer these cases."
               + (f"\n\n⚠️ **{staged['review']}** of them are *Review required*: the newest "
                  "upload no longer flags the day." if staged.get("review") else ""))
    b = st.columns([1, 1, 4])
    if b[0].button("Confirm waive", type="primary", key="wv_confirm"):
        go = waive.take(st.session_state)
        if go:
            res = data.waive_cases(c, go["ids"], actor, go["reason"])
            msg = f"Waived {res['waived']} case(s) as Absent."
            if res["skipped"]:
                msg += (f" {res['skipped']} skipped: a team leader answered (or HRBP decided) "
                        "them after the list was loaded.")
            st.session_state["wv_result"] = msg
        st.rerun()
    if b[1].button("Cancel", key="wv_cancel"):
        waive.cancel(st.session_state)
        st.rerun()


def render(c, actor) -> None:
    st.subheader("Waive unanswered cases")
    st.caption("Closes cases **no team leader has answered yet** as **Absent**. Cases a team "
               "leader already answered are not listed. A waived case leaves the team leader's "
               "link; you can still change it later with an HRBP Override on the Dashboard.")
    done = st.session_state.pop("wv_result", None)
    if done:
        st.success(done)
    rows = data.list_waivable_cases(c)
    if not rows:
        waive.cancel(st.session_state)
        st.info("No unanswered cases to waive.")
        return
    # The filters, list and reason stay on screen while a waive awaits confirmation (so their
    # state is kept); only the action buttons are locked until it is confirmed or cancelled.
    shown = _filters(rows)
    st.caption(f"{len(shown)} of {len(rows)} unanswered case(s) shown.")
    ticked = _table(shown) if shown else []
    reason = st.text_input("Reason (required)", key="wv_reason", max_chars=waive.MAX_REASON,
                           placeholder="e.g. TL on leave")
    locked = waive.pending(st.session_state) is not None
    ok = bool(reason.strip()) and not locked
    b = st.columns(2)
    pick = None
    if b[0].button(f"Waive selected ({len(ticked)})", disabled=not (ticked and ok), key="wv_sel"):
        pick = ticked
    if b[1].button(f"Waive all shown ({len(shown)})", disabled=not (shown and ok), key="wv_all"):
        pick = shown
    if pick:
        try:
            waive.stage(st.session_state, pick, reason)
        except ValueError as e:
            st.error(str(e))
    staged = waive.pending(st.session_state)
    if staged:
        _confirm(c, actor, staged)
