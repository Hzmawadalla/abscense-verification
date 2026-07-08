"""Attendance Verification — Streamlit app (SPEC §2).

Two access layers, routed at entry:
  • TL layer   — opened via unique link  …/?t=<token>  (no login; token is the credential)
  • HRBP layer — per-user email+password (streamlit-authenticator, backed by hrbp_users)

Run locally:  SUPABASE_DB_URL=postgresql://... streamlit run streamlit_app.py
On Streamlit Community Cloud, set SUPABASE_DB_URL (and cookie secrets) in the app's Secrets.
"""
import os

import streamlit as st

from app import data
from app.dingtalk import DingTalkClient
from app.report import build_reconciled_report
from app.storage import StorageClient, object_path, validate_upload
from ingestion import loader
from ingestion.config import load_aliases, load_dingtalk_ids
from ingestion.db_psycopg import PsycopgDB
from ingestion.reference import parse_reference
from ingestion.summary import ingest_summary

st.set_page_config(page_title="Attendance Verification", page_icon="🗓️", layout="wide")

# Verdict dropdown (label -> stored enum code), shared by the TL page and HRBP override.
VERDICTS = {
    "Present": "present",
    "Annual Leave": "annual_leave",
    "Unpaid Leave": "unpaid_leave",
    "Sick Leave": "sick_leave",
    "Absent": "absent",
    "Half Day": "half_day",
}
# Reverse map for display/export; includes the legacy 'leave' value for any historical rows.
VERDICT_LABEL = {code: label for label, code in VERDICTS.items()}
VERDICT_LABEL["leave"] = "On Leave"


@st.cache_resource
def get_conn():
    import psycopg
    dsn = st.secrets.get("SUPABASE_DB_URL", os.environ.get("SUPABASE_DB_URL"))
    if not dsn:
        st.error("SUPABASE_DB_URL is not configured (Streamlit Secrets or env).")
        st.stop()
    # autocommit=True so reads/writes never leave an idle-in-transaction session: if the app
    # process is killed (reboot/redeploy), the pooled connection would otherwise linger holding
    # locks and block later writes until statement_timeout. Multi-statement writes that need
    # atomicity use explicit `with conn.transaction()` blocks (see the ingest tab).
    cn = psycopg.connect(dsn, autocommit=True, prepare_threshold=None)
    # Safety net for any transaction that does linger (e.g. process killed mid-ingest): make the
    # server auto-abort it (and release its locks) quickly instead of holding until the timeout.
    cn.execute("set idle_in_transaction_session_timeout = '30s'")
    return cn


def conn():
    c = get_conn()
    try:  # revive a dropped idle connection (Supabase closes idle sockets)
        c.execute("select 1")
    except Exception:
        get_conn.clear()
        c = get_conn()
    return c


def dingtalk_client():
    ak = st.secrets.get("DINGTALK_APP_KEY", os.environ.get("DINGTALK_APP_KEY"))
    sk = st.secrets.get("DINGTALK_APP_SECRET", os.environ.get("DINGTALK_APP_SECRET"))
    ag = st.secrets.get("DINGTALK_AGENT_ID", os.environ.get("DINGTALK_AGENT_ID"))
    if not (ak and sk and ag):
        return None
    return DingTalkClient(ak, sk, ag)


def storage_client():
    url = st.secrets.get("SUPABASE_URL", os.environ.get("SUPABASE_URL"))
    key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY", os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))
    if not (url and key):
        return None
    return StorageClient(url, key)


def send_tl_link(c, client, mgr, base):
    """Rotate a link and DM it to the TL over DingTalk; record the attempt either way."""
    token = data.generate_manager_link(c, mgr["id"])
    link = f"{base}/?t={token}"
    try:
        resp = client.send_link(mgr["dingtalk_userid"], mgr["name"], link, mgr["open_cases"])
        data.record_notification(c, mgr["id"], "dingtalk", mgr["open_cases"], "sent",
                                 provider_message_id=str(resp.get("task_id")))
        return True, None
    except Exception as e:  # noqa: BLE001 — surface any dispatch failure, never silently drop
        data.record_notification(c, mgr["id"], "dingtalk", mgr["open_cases"], "failed", error=str(e))
        return False, str(e)


# ============================================================ TL LAYER (token link)
def render_tl(token: str):
    c = conn()
    mgr = data.manager_by_token(c, token)
    if not mgr:
        st.error("This link is invalid or has been rotated. Please contact HR for a new link.")
        return

    st.title("Attendance Verification")
    st.caption(f"Team Leader: **{mgr['name'] or mgr['crm']}** — please confirm each flagged day for your team.")
    cases = data.open_cases_for_manager(c, mgr["id"])
    if not cases:
        st.success("You're all caught up — no open cases. Thank you!")
        return

    st.write(f"**{len(cases)}** case(s) awaiting your confirmation.")
    with st.form("verify"):
        choices = {}
        for cs in cases:
            hd = " · ½ day" if cs["is_half_day"] else ""
            st.markdown(f"**{cs['employee_name']}** — {cs['work_date']} · flagged as "
                        f"*{cs['source_status']}*{hd}")
            col1, col2 = st.columns([1, 2])
            default = 0
            if cs["manager_status"]:
                order = list(VERDICTS.values())
                default = order.index(cs["manager_status"]) if cs["manager_status"] in order else 0
            verdict = col1.selectbox("Verdict", list(VERDICTS.keys()), index=default, key=f"v{cs['id']}")
            comment = col2.text_input("Comment (evidence, e.g. approved leave email)",
                                      value=cs["manager_comment"] or "", key=f"c{cs['id']}")
            upl = col2.file_uploader("Attach proof (pdf/jpg/png)", type=["pdf", "jpg", "jpeg", "png"],
                                     key=f"f{cs['id']}")
            choices[cs["id"]] = (VERDICTS[verdict], None, comment, upl)
            st.divider()
        if st.form_submit_button("Submit all", type="primary"):
            actor = f"tl:{mgr['crm']}"
            sc = storage_client()
            ok = stale = files = 0
            for cid, (ms, lt, cm, upl) in choices.items():
                if not data.submit_verdict(c, cid, ms, lt, cm, actor):
                    stale += 1
                    continue
                ok += 1
                if upl is not None and sc is not None:
                    try:
                        validate_upload(upl.type, upl.size)
                        path = sc.upload(object_path(cid, upl.name), upl.getvalue(), upl.type)
                        data.add_attachment(c, cid, path, upl.name, upl.type, upl.size)
                        files += 1
                    except Exception as e:  # noqa: BLE001 — show the TL why an attachment didn't stick
                        st.warning(f"Attachment for one case failed: {e}")
            msg = f"Saved {ok} response(s)."
            msg += f" {files} attachment(s)." if files else ""
            msg += f" {stale} were already closed by HR." if stale else ""
            st.success(msg)
            st.rerun()


# ============================================================ HRBP LAYER (login)
def hrbp_authenticator():
    import streamlit_authenticator as stauth
    creds = data.hrbp_credentials(conn())
    if not creds["usernames"]:
        st.warning("No HRBP users exist yet. Create the first one with tools/create_hrbp.py.")
        st.stop()
    cookie = st.secrets.get("AUTH_COOKIE_KEY", os.environ.get("AUTH_COOKIE_KEY", "change-me"))
    return stauth.Authenticate(creds, "attendance_auth", cookie, cookie_expiry_days=1)


def render_hrbp():
    authenticator = hrbp_authenticator()
    authenticator.login(location="main")
    status = st.session_state.get("authentication_status")
    if status is False:
        st.error("Incorrect email or password.")
        return
    if status is None:
        st.info("Please log in to access the HRBP dashboard.")
        return

    actor = f"hrbp:{st.session_state.get('username')}"
    with st.sidebar:
        st.write(f"Signed in as **{st.session_state.get('name')}**")
        authenticator.logout("Log out", "sidebar")

    tab_dash, tab_ingest, tab_exc, tab_links, tab_close, tab_export = st.tabs(
        ["📋 Dashboard", "⬆️ Ingest", "⚠️ Exceptions", "🔗 TL links", "🔒 Period close", "📤 Export"])
    c = conn()

    with tab_dash:
        counts = data.counts_by_status(c)
        m = st.columns(3)
        m[0].metric("Open", counts.get("open", 0))
        m[1].metric("Responded", counts.get("manager_responded", 0))
        m[2].metric("Closed", counts.get("closed", 0))
        status_filter = st.selectbox("Show", ["manager_responded", "open", "closed", "(all)"])
        rows = data.list_cases(c, status=None if status_filter == "(all)" else status_filter)
        st.dataframe(rows, use_container_width=True, hide_index=True)

        st.subheader("Resolve a responded case")
        responded = [r for r in data.list_cases(c, status="manager_responded")]
        if responded:
            label = {f"{r['employee_name']} · {r['work_date']} · TL said {r['manager_status']}": r
                     for r in responded}
            pick = st.selectbox("Case", list(label))
            r = label[pick]
            st.write(f"TL verdict: **{r['manager_status']}** · comment: {r['manager_comment'] or '—'}")
            sc = storage_client()
            for a in data.list_attachments(c, r["id"]):
                if sc:
                    try:
                        st.markdown(f"📎 [{a['filename']}]({sc.signed_url(a['storage_path'])})")
                    except Exception as e:  # noqa: BLE001
                        st.caption(f"📎 {a['filename']} (link error: {e})")
                else:
                    st.caption(f"📎 {a['filename']} (storage not configured)")
            cc = st.columns(2)
            if cc[0].button("✅ Close as-is (accept TL verdict)"):
                data.close_case(c, r["id"], actor)
                st.rerun()
            with cc[1]:
                ov = st.selectbox("Override to", list(VERDICTS.keys()), key="ov")
                ovc = st.text_input("Reason (required for override)", key="ovc")
                if st.button("Override & close"):
                    if not ovc.strip():
                        st.error("Override requires a reason.")
                    else:
                        data.close_case(c, r["id"], actor, final_status=VERDICTS[ov], comment=ovc)
                        st.rerun()
        else:
            st.caption("No responded cases awaiting resolution.")

    with tab_ingest:
        st.write("Upload this period's workbooks to create verification cases. The **HC** and "
                 "**Structure** tabs come from the HR export; the **Summary Report** tab comes from "
                 "the attendance tool. If you have one combined workbook, upload it in either box.")
        ref_up = st.file_uploader("Reference workbook — HC + Structure tabs (.xlsx)",
                                  type=["xlsx"], key="ref_wb")
        att_up = st.file_uploader("Attendance report — Summary Report tab (.xlsx)",
                                  type=["xlsx"], key="att_wb")
        year = st.number_input("Year for the date columns", 2024, 2100, 2026)
        if st.button("Parse & load", type="primary"):
            if not (ref_up or att_up):
                st.warning("Upload at least one workbook.")
                st.stop()
            ref_src = ref_up or att_up      # single combined workbook → use it for both
            att_src = att_up or ref_up

            def _save(uploaded):
                path = f"/tmp/{uploaded.name}"
                with open(path, "wb") as f:
                    f.write(uploaded.getbuffer())
                return path

            ref_path = _save(ref_src)
            att_path = ref_path if att_src is ref_src else _save(att_src)

            try:
                ref = parse_reference(ref_path, aliases=load_aliases())
            except KeyError as e:
                st.error(f"The reference workbook is missing a required tab ({e}). It must contain "
                         "**HC** and **Structure** sheets (from the HR export).")
                st.stop()
            try:
                res = ingest_summary(att_path, ref, year=int(year))
            except (KeyError, ValueError) as e:
                st.error(f"The attendance workbook can't be parsed ({e}). It must contain a "
                         "**Summary Report** sheet (from the attendance tool).")
                st.stop()

            with c.transaction():  # atomic; commits on success WITHOUT closing the pooled
                db = PsycopgDB(c)   # connection (psycopg3's `with conn:` would close it)
                loader.load_reference(db, ref)
                summary = loader.load_ingestion(db, res, reference=ref, source_filename=att_src.name)
            applied = data.set_dingtalk_ids(c, load_dingtalk_ids())
            st.success(f"Loaded {summary.cases} cases, {summary.exceptions} exceptions "
                       f"({ref.stats['mapped_employees']}/{ref.stats['employees']} employees mapped)."
                       + (f" Applied {applied} DingTalk id(s)." if applied else ""))

    with tab_exc:
        exc = data.list_exceptions(c, resolved=False)
        st.write(f"**{len(exc)}** open exception(s) — the 'fix Structure' + data-quality worklist.")
        st.dataframe(exc, use_container_width=True, hide_index=True)

    with tab_links:
        st.write("Generate each TL's unique link and DM it via DingTalk.")
        base = st.text_input("App base URL", st.secrets.get("APP_BASE_URL", "https://your-app.streamlit.app"))
        client = dingtalk_client()
        if client is None:
            st.warning("DingTalk not configured (DINGTALK_APP_KEY / _SECRET / _AGENT_ID). "
                       "You can still generate links to copy manually.")
        overview = data.managers_overview(c)

        if client and st.button("📨 Send to ALL TLs with open cases", type="primary"):
            sent = fail = 0
            for mgr in overview:
                if mgr["dingtalk_userid"] and mgr["open_cases"]:
                    ok, _ = send_tl_link(c, client, mgr, base)
                    sent += ok
                    fail += (not ok)
            st.success(f"DingTalk: sent {sent}, failed {fail}.")

        for mgr in overview:
            cols = st.columns([3, 1, 1, 3])
            uid = mgr["dingtalk_userid"] or "— no userid —"
            cols[0].write(f"**{mgr['name'] or mgr['crm']}** · {mgr['open_cases']} open · dingtalk: `{uid}`")
            if cols[1].button("Link", key=f"gl{mgr['id']}"):
                tok = data.generate_manager_link(c, mgr["id"])
                st.session_state[f"link{mgr['id']}"] = f"{base}/?t={tok}"
            can_send = bool(client and mgr["dingtalk_userid"] and mgr["open_cases"])
            if cols[2].button("Send", key=f"snd{mgr['id']}", disabled=not can_send):
                ok, err = send_tl_link(c, client, mgr, base)
                st.success(f"Sent to {mgr['name']}.") if ok else st.error(f"Failed: {err}")
            if st.session_state.get(f"link{mgr['id']}"):
                cols[3].code(st.session_state[f"link{mgr['id']}"], language=None)

    with tab_close:
        st.warning("Closes every remaining open case as **Absent** (period cutoff). Cannot be undone.")
        if st.button("Close the period now", type="primary"):
            n = data.close_open_month(c, actor)
            st.success(f"Closed {n} open case(s) as Absent.")

    with tab_export:
        st.write("Re-upload this period's attendance workbook to get it back with every **closed** "
                 "case updated to its final verdict, plus a **Changes** sheet of before → after.")
        exp_up = st.file_uploader("Attendance report — Summary Report tab (.xlsx)",
                                  type=["xlsx"], key="exp_wb")
        exp_year = st.number_input("Year for the date columns", 2024, 2100, 2026, key="exp_year")
        if exp_up and st.button("Build reconciled report", type="primary"):
            closed = data.list_closed_cases(c)
            if not closed:
                st.warning("No closed cases yet — verify and close cases before exporting.")
            else:
                tmp = f"/tmp/{exp_up.name}"
                with open(tmp, "wb") as f:
                    f.write(exp_up.getbuffer())
                try:
                    xlsx = build_reconciled_report(tmp, closed, VERDICT_LABEL, year=int(exp_year))
                except (KeyError, ValueError) as e:
                    st.error(f"Couldn't build the report ({e}). Upload the same workbook that has "
                             "the **Summary Report** tab.")
                else:
                    st.success(f"Reconciled {len(closed)} closed case(s). Download below.")
                    st.download_button("⬇️ Download reconciled report", data=xlsx,
                                       file_name="Attendance_Reconciled.xlsx",
                                       mime="application/vnd.openxmlformats-officedocument."
                                            "spreadsheetml.sheet")


# ============================================================ ROUTER
token = st.query_params.get("t")
if token:
    render_tl(token)
else:
    render_hrbp()
