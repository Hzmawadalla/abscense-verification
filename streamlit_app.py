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
from ingestion import loader
from ingestion.config import load_aliases
from ingestion.db_psycopg import PsycopgDB
from ingestion.reference import parse_reference
from ingestion.summary import ingest_summary

st.set_page_config(page_title="Attendance Verification", page_icon="🗓️", layout="wide")

VERDICTS = {"Present": "present", "On Leave": "leave", "Absent Confirmed": "absent"}


@st.cache_resource
def get_conn():
    import psycopg
    dsn = st.secrets.get("SUPABASE_DB_URL", os.environ.get("SUPABASE_DB_URL"))
    if not dsn:
        st.error("SUPABASE_DB_URL is not configured (Streamlit Secrets or env).")
        st.stop()
    return psycopg.connect(dsn, autocommit=False)


def conn():
    c = get_conn()
    try:  # revive a dropped idle connection (Supabase closes idle sockets)
        c.execute("select 1")
    except Exception:
        get_conn.clear()
        c = get_conn()
    return c


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
            leave_type = None
            if VERDICTS[verdict] == "leave":
                leave_type = col2.text_input("Leave type", value=cs["leave_type"] or "",
                                             key=f"l{cs['id']}", placeholder="annual / sick / unpaid …")
            choices[cs["id"]] = (VERDICTS[verdict], leave_type, comment)
            st.divider()
        if st.form_submit_button("Submit all", type="primary"):
            actor = f"tl:{mgr['crm']}"
            ok = stale = 0
            for cid, (ms, lt, cm) in choices.items():
                if data.submit_verdict(c, cid, ms, lt, cm, actor):
                    ok += 1
                else:
                    stale += 1
            st.success(f"Saved {ok} response(s)." + (f" {stale} were already closed by HR." if stale else ""))
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

    tab_dash, tab_ingest, tab_exc, tab_links, tab_close = st.tabs(
        ["📋 Dashboard", "⬆️ Ingest", "⚠️ Exceptions", "🔗 TL links", "🔒 Period close"])
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
        st.write("Upload the attendance workbook to create this period's cases.")
        up = st.file_uploader("Workbook (.xlsx)", type=["xlsx"])
        year = st.number_input("Year for the date columns", 2024, 2100, 2026)
        if up and st.button("Parse & load", type="primary"):
            tmp = f"/tmp/{up.name}"
            with open(tmp, "wb") as f:
                f.write(up.getbuffer())
            ref = parse_reference(tmp, aliases=load_aliases())
            res = ingest_summary(tmp, ref, year=int(year))
            with c:  # one transaction; commits on success
                db = PsycopgDB(c)
                loader.load_reference(db, ref)
                summary = loader.load_ingestion(db, res, reference=ref, source_filename=up.name)
            st.success(f"Loaded {summary.cases} cases, {summary.exceptions} exceptions "
                       f"({ref.stats['mapped_employees']}/{ref.stats['employees']} employees mapped).")

    with tab_exc:
        exc = data.list_exceptions(c, resolved=False)
        st.write(f"**{len(exc)}** open exception(s) — the 'fix Structure' + data-quality worklist.")
        st.dataframe(exc, use_container_width=True, hide_index=True)

    with tab_links:
        st.write("Generate or rotate the unique verification link for each Team Leader.")
        base = st.text_input("App base URL", st.secrets.get("APP_BASE_URL", "https://your-app.streamlit.app"))
        for mgr in data.list_managers(c):
            cols = st.columns([3, 1, 3])
            cols[0].write(f"**{mgr['name'] or mgr['crm']}** ({mgr['email'] or 'no email'})")
            if cols[1].button("Generate", key=f"gl{mgr['id']}"):
                tok = data.generate_manager_link(c, mgr["id"])
                st.session_state[f"link{mgr['id']}"] = f"{base}/?t={tok}"
            if st.session_state.get(f"link{mgr['id']}"):
                cols[2].code(st.session_state[f"link{mgr['id']}"], language=None)

    with tab_close:
        st.warning("Closes every remaining open case as **Absent** (period cutoff). Cannot be undone.")
        if st.button("Close the period now", type="primary"):
            n = data.close_open_month(c, actor)
            st.success(f"Closed {n} open case(s) as Absent.")


# ============================================================ ROUTER
token = st.query_params.get("t")
if token:
    render_tl(token)
else:
    render_hrbp()
