"""Data access for the Streamlit app. Thin functions over a psycopg connection, all schema-qualified
to `attendance`. Verification writes use optimistic locking and always append to the audit log."""
import json

from psycopg.rows import dict_row

from app import security


# --------------------------------------------------------------------------- audit
def _audit(cur, case_id, actor, action, old, new):
    cur.execute(
        "insert into attendance.audit_log (case_id, actor, action, old_value, new_value) "
        "values (%s, %s, %s, %s, %s)",
        (case_id, actor, action, json.dumps(old, default=str) if old is not None else None,
         json.dumps(new, default=str) if new is not None else None),
    )


# A case "has history" once anything beyond its ingestion happened to it: a live answer, any
# non-open state, any attachment (voided ones included) or any audit entry. A voided case has no
# live manager_status but still carries its old answer in the audit log and its voided evidence,
# so checking the live answer alone would wrongly treat it as untouched. Expects alias `c`.
CASE_HAS_HISTORY = (
    "(c.manager_status is not null or c.status <> 'open' "
    " or exists (select 1 from attendance.case_attachments a where a.case_id = c.id) "
    " or exists (select 1 from attendance.audit_log l where l.case_id = c.id))")


# --------------------------------------------------------------------------- HRBP auth
def hrbp_credentials(conn) -> dict:
    """Build the streamlit-authenticator credentials dict from active HRBP users."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select email, name, password_hash from attendance.hrbp_users "
                    "where active and password_hash is not null")
        rows = cur.fetchall()
    return {"usernames": {
        r["email"]: {"email": r["email"], "name": r["name"] or r["email"], "password": r["password_hash"]}
        for r in rows
    }}


def create_hrbp(conn, email, name, password) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "insert into attendance.hrbp_users (email, name, password_hash, active) "
            "values (%s, %s, %s, true) "
            "on conflict (email) do update set name = excluded.name, "
            "password_hash = excluded.password_hash, active = true",
            (email.lower().strip(), name, security.hash_password(password)),
        )
    conn.commit()


# --------------------------------------------------------------------------- TL token
def manager_by_token(conn, token):
    """Resolve a raw TL token to their manager row, or None."""
    if not token:
        return None
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select id, crm, name, email, access_token_hash, active "
                    "from attendance.managers where access_token_hash = %s and active",
                    (security.hash_token(token),))
        return cur.fetchone()


def generate_manager_link(conn, manager_id) -> str:
    """Return the manager's existing TL link token, minting and storing one only if none exists.
    Idempotent: repeat calls (copy, download-all, re-email) return the same token, so links already
    distributed keep working. The token is stored encrypted (key in app secrets, not the DB) and
    decrypted here to reproduce the link. Use rotate_manager_link to deliberately replace it."""
    with conn.cursor() as cur:
        cur.execute("select access_token_enc from attendance.managers where id = %s", (manager_id,))
        row = cur.fetchone()
        if row and row[0]:
            existing = security.decrypt_token(row[0])
            if existing:
                return existing   # unreadable ciphertext (e.g. rotated key) falls through to mint
        token = security.generate_token()
        cur.execute("update attendance.managers set access_token_enc = %s, access_token_hash = %s "
                    "where id = %s", (security.encrypt_token(token), security.hash_token(token),
                                      manager_id))
    conn.commit()
    return token


def rotate_manager_link(conn, manager_id) -> str:
    """Mint a brand-new TL token, overwriting any existing one — this INVALIDATES the link already
    sent. Deliberate rotation only (the pre-idempotency generate_manager_link behavior)."""
    token = security.generate_token()
    with conn.cursor() as cur:
        cur.execute("update attendance.managers set access_token_enc = %s, access_token_hash = %s "
                    "where id = %s", (security.encrypt_token(token), security.hash_token(token),
                                      manager_id))
    conn.commit()
    return token


# --------------------------------------------------------------------------- cases (TL side)
def open_cases_for_manager(conn, manager_id):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select c.id, c.work_date, c.source_status, c.is_half_day, c.status, "
            "       c.manager_status, c.leave_type, c.manager_comment, "
            "       e.name as employee_name, e.crm as employee_crm "
            "from attendance.cases c join attendance.employees e on e.id = c.employee_id "
            "where c.manager_id = %s and c.status in ('open','manager_responded') "
            "order by e.name, c.work_date",
            (manager_id,))
        return cur.fetchall()


def submit_verdict(conn, case_id, manager_status, leave_type, comment, actor) -> bool:
    """Write a TL verdict once. Returns False if the case is not 'open' (already validated or closed).

    The `where status = 'open'` guard makes validation one-time at the database level, so a reload,
    double-click, or replayed form cannot overwrite an answer the TL already submitted."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select status, manager_status, leave_type, manager_comment "
                    "from attendance.cases where id = %s", (case_id,))
        old = cur.fetchone()
        if old is None or old["status"] != "open":
            return False
        cur.execute(
            "update attendance.cases set status = 'closed', manager_status = %s, final_status = %s, "
            "leave_type = %s, manager_comment = %s, manager_responded_at = now(), "
            "closed_by = 'tl', closed_at = now() "
            "where id = %s and status = 'open'",
            (manager_status, manager_status, leave_type, comment, case_id))
        if cur.rowcount == 0:
            conn.rollback()
            return False
        _audit(cur, case_id, actor, "tl_verdict", old,
               {"manager_status": manager_status, "final_status": manager_status,
                "leave_type": leave_type, "manager_comment": comment})
    conn.commit()
    return True


# --------------------------------------------------------------------------- void / reopen (HRBP)
def list_tl_submitted_cases(conn, manager_id):
    """Cases this TL finalized themselves (closed_by='tl') — the set an HRBP may void."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select c.id, c.work_date, c.source_status, c.is_half_day, c.manager_status, "
            "       c.leave_type, c.manager_comment, "
            "       e.name as employee_name, e.crm as employee_crm "
            "from attendance.cases c join attendance.employees e on e.id = c.employee_id "
            "where c.manager_id = %s and c.closed_by = 'tl' and c.status = 'closed' "
            "order by e.name, c.work_date",
            (manager_id,))
        return cur.fetchall()


def reopen_tl_cases(conn, case_ids, actor, reason) -> dict:
    """Void a TL's submitted verdicts so they can resubmit on their existing link.

    Reopens only cases the TL finalized themselves (closed_by='tl', status='closed'); any other
    case in the list is skipped and counted. For each reopened case it clears the live verdict
    fields (the exact inverse of submit_verdict) and appends an audit entry whose snapshot keeps
    the voided answer — verdict, comment, answer time. Nothing is deleted: the case's attachments
    are marked voided (kept for audit, no longer shown as current evidence) and their storage
    objects are left in place.

    Returns {'reopened': n, 'skipped': n, 'attachments_voided': n}.
    """
    if not case_ids:
        return {"reopened": 0, "skipped": 0, "attachments_voided": 0}
    reopened = skipped = voided = 0
    with conn.cursor(row_factory=dict_row) as cur:
        for cid in case_ids:
            cur.execute("select status, manager_status, final_status, leave_type, "
                        "manager_comment, manager_responded_at, closed_by "
                        "from attendance.cases where id = %s", (cid,))
            old = cur.fetchone()
            if old is None or old["closed_by"] != "tl" or old["status"] != "closed":
                skipped += 1
                continue
            cur.execute(
                "update attendance.cases set status = 'open', manager_status = null, "
                "final_status = null, leave_type = null, manager_comment = null, "
                "manager_responded_at = null, closed_by = null, closed_at = null "
                "where id = %s and closed_by = 'tl' and status = 'closed'", (cid,))
            if cur.rowcount == 0:            # raced with another writer since the read
                skipped += 1
                continue
            cur.execute(
                "update attendance.case_attachments set voided_at = now(), voided_by = %s "
                "where case_id = %s and voided_at is null returning id", (actor, cid))
            att_ids = [r["id"] for r in cur.fetchall()]
            voided += len(att_ids)
            reopened += 1
            _audit(cur, cid, actor, "hrbp_void", dict(old),
                   {"status": "open", "reason": reason, "voided_attachments": att_ids})
    conn.commit()
    return {"reopened": reopened, "skipped": skipped, "attachments_voided": voided}


# --------------------------------------------------------------------------- cases (HRBP side)
def list_cases(conn, status=None, team=None, manager_id=None):
    q = ("select c.id, c.work_date, c.source_status, c.status, c.manager_status, c.leave_type, "
         "       c.manager_comment, c.final_status, c.closed_by, "
         "       c.hrbp_override_note, c.hrbp_override_by, c.hrbp_override_at, "
         "       e.name as employee_name, e.crm as employee_crm, e.team, m.name as manager_name "
         "from attendance.cases c "
         "join attendance.employees e on e.id = c.employee_id "
         "left join attendance.managers m on m.id = c.manager_id where true ")
    params = []
    if status:
        q += "and c.status = %s "; params.append(status)
    if team:
        q += "and e.team = %s "; params.append(team)
    if manager_id:
        q += "and c.manager_id = %s "; params.append(manager_id)
    q += "order by c.status, e.name, c.work_date"
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(q, tuple(params))
        return cur.fetchall()


def list_closed_cases(conn):
    """Finalized cases (for the reconciled export): CRM x date x source -> final verdict."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select e.crm as employee_crm, e.name as employee_name, m.name as manager_name, "
            "       c.work_date, c.source_status, c.final_status, c.closed_by, c.manager_comment "
            "from attendance.cases c "
            "join attendance.employees e on e.id = c.employee_id "
            "left join attendance.managers m on m.id = c.manager_id "
            "where c.status = 'closed' and c.final_status is not null "
            "order by e.crm, c.work_date")
        return cur.fetchall()


def close_case(conn, case_id, actor, final_status=None, final_leave_type=None, comment=None) -> bool:
    """Close a case. final_status=None means 'accept the TL verdict'; else an HRBP Override.

    An override records its value, the HRBP actor, the time and the reason in the hrbp_override_*
    columns. The TL's verdict, comment and answer time are never modified, and the audit snapshot
    keeps them together with any previous override, so repeated overrides stay traceable."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select status, manager_status, leave_type, manager_comment, "
                    "manager_responded_at, final_status, final_leave_type, closed_by, "
                    "hrbp_override_note, hrbp_override_by, hrbp_override_at "
                    "from attendance.cases where id = %s", (case_id,))
        old = cur.fetchone()
        if old is None:  # allow overriding an already-closed (TL-finalized) case
            return False
        if final_status:
            cur.execute(
                "update attendance.cases set status = 'closed', final_status = %s, "
                "final_leave_type = %s, closed_by = 'hrbp', closed_at = now(), "
                "hrbp_override_note = %s, hrbp_override_by = %s, hrbp_override_at = now() "
                "where id = %s",
                (final_status, final_leave_type, comment, actor, case_id))
            final, leave = final_status, final_leave_type
        else:
            final, leave = old["manager_status"], old["leave_type"]
            cur.execute(
                "update attendance.cases set status = 'closed', final_status = %s, "
                "final_leave_type = %s, closed_by = 'hrbp', closed_at = now() where id = %s",
                (final, leave, case_id))
        if cur.rowcount == 0:
            conn.rollback()
            return False
        new = {"final_status": final, "final_leave_type": leave}
        if final_status:
            new["hrbp_override_note"] = comment
        _audit(cur, case_id, actor, "hrbp_override" if final_status else "hrbp_close", old, new)
    conn.commit()
    return True


def close_open_month(conn, actor) -> int:
    """Period close: stand every remaining open case as Absent (SPEC §6.5)."""
    with conn.cursor() as cur:
        cur.execute(
            "update attendance.cases set status = 'closed', final_status = 'absent', "
            "closed_by = 'hrbp_cutoff', closed_at = now() "
            "where status in ('open','manager_responded') returning id")
        ids = [r[0] for r in cur.fetchall()]
        for cid in ids:
            _audit(cur, cid, actor, "auto_close_absent", None, {"final_status": "absent"})
    conn.commit()
    return len(ids)


def list_reference_gaps(conn):
    """Undated exceptions = HC/Structure completeness gaps (staff not producing verifiable cases)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select distinct crm, reason, raw_value as detail "
                    "from attendance.ingestion_exceptions "
                    "where work_date is null and resolved = false order by reason, crm")
        return cur.fetchall()


def list_exceptions(conn, resolved=False):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select id, crm, work_date, raw_value, reason, resolved, created_at "
                    "from attendance.ingestion_exceptions where resolved = %s "
                    "order by reason, crm", (resolved,))
        return cur.fetchall()


def list_managers(conn):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select id, crm, name, email, "
                    "(access_token_hash is not null) as has_link from attendance.managers "
                    "where active order by name")
        return cur.fetchall()


def counts_by_status(conn):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select status, count(*) as n from attendance.cases group by status")
        return {r["status"]: r["n"] for r in cur.fetchall()}


# --------------------------------------------------------------------------- DingTalk / notifications
def set_dingtalk_ids(conn, mapping) -> int:
    """Apply a {CRM -> DingTalk userid} mapping onto managers (case-insensitive on CRM)."""
    if not mapping:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "update attendance.managers set dingtalk_userid = %s where lower(crm) = lower(%s)",
            [(uid, crm) for crm, uid in mapping.items()])
    conn.commit()
    return len(mapping)


def managers_overview(conn):
    """Each active TL with their open-case count, DingTalk userid, and whether a link exists."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select m.id, m.crm, m.name, m.email, m.dingtalk_userid, "
            "       (m.access_token_hash is not null) as has_link, "
            "       count(c.id) filter (where c.status in ('open','manager_responded')) as open_cases "
            "from attendance.managers m "
            "left join attendance.cases c on c.manager_id = m.id "
            "where m.active group by m.id order by open_cases desc, m.name")
        return cur.fetchall()


def record_notification(conn, manager_id, channel, case_count, status,
                        provider_message_id=None, error=None, ingestion_run_id=None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "insert into attendance.notifications "
            "(manager_id, ingestion_run_id, channel, case_count, status, provider_message_id, error) "
            "values (%s, %s, %s, %s, %s, %s, %s)",
            (manager_id, ingestion_run_id, channel, case_count, status, provider_message_id, error))
    conn.commit()


# --------------------------------------------------------------------------- attachments
def add_attachment(conn, case_id, storage_path, filename, content_type, size_bytes) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "insert into attendance.case_attachments "
            "(case_id, storage_path, filename, content_type, size_bytes) "
            "values (%s, %s, %s, %s, %s)",
            (case_id, storage_path, filename, content_type, size_bytes))
    conn.commit()


def list_attachments(conn, case_id, include_voided=False):
    """A case's evidence. Voided files (from a voided TL answer) are history, not current
    evidence, so they are left out unless include_voided=True."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select storage_path, filename, content_type, voided_at, voided_by "
                    "from attendance.case_attachments where case_id = %s "
                    "and (%s or voided_at is null) order by uploaded_at",
                    (case_id, include_voided))
        return cur.fetchall()


def attachments_for_cases(conn, case_ids):
    """Every current (non-voided) attachment for the given cases, in one query.

    The HRBP evidence view renders many cases at once; fetching per case would be an N+1.
    Returns flat rows carrying case_id — see `app.attachments.group_by_case`.
    """
    ids = list(case_ids)
    if not ids:
        return []
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select case_id, storage_path, filename, content_type "
                    "from attendance.case_attachments where case_id = any(%s) "
                    "and voided_at is null order by case_id, uploaded_at", (ids,))
        return cur.fetchall()


# --------------------------------------------------------------------------- upload management
def list_uploads(conn):
    """Every ingestion run with its case counts (newest first), for the HRBP Uploads panel."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select r.id, r.source_filename, r.created_at, "
            "       count(c.id) as total, "
            "       count(c.id) filter (where c.manager_status is not null) as verified, "
            "       count(c.id) filter (where c.status = 'open') as open, "
            f"       count(c.id) filter (where {CASE_HAS_HISTORY}) as protected "
            "from attendance.ingestion_runs r "
            "left join attendance.cases c on c.ingestion_run_id = r.id "
            "group by r.id, r.source_filename, r.created_at "
            "order by r.created_at desc")
        return cur.fetchall()


def _count_with_history(cur, where_sql, params=()):
    cur.execute(f"select count(*) from attendance.cases c where {where_sql} "
                f"and {CASE_HAS_HISTORY}", params)
    return cur.fetchone()[0]


def remove_upload(conn, run_id) -> dict:
    """Delete one upload — only if none of its cases has history (CASE_HAS_HISTORY).

    Returns {'refused': n} without deleting anything when n case(s) carry an answer, evidence or
    audit history; otherwise deletes the cases (by ingestion_run_id) then the run (exceptions
    cascade) and returns {'cases_deleted': n}. The database backs this up: a case with
    attachments or audit entries cannot be deleted (those foreign keys are RESTRICT)."""
    with conn.cursor() as cur:
        protected = _count_with_history(cur, "c.ingestion_run_id = %s", (run_id,))
        if protected:
            conn.rollback()
            return {"refused": protected}
        cur.execute("select count(*) from attendance.cases where ingestion_run_id = %s", (run_id,))
        total = cur.fetchone()[0]
        cur.execute("delete from attendance.cases where ingestion_run_id = %s", (run_id,))
        cur.execute("delete from attendance.ingestion_runs where id = %s", (run_id,))
    conn.commit()
    return {"cases_deleted": total}


def reset_all_cases(conn) -> dict:
    """Clear all case data (cases, ingestion exceptions, ingestion runs) for a fresh start — only
    while no case has history. Managers, employees, TL tokens, status vocabulary, and HRBP logins
    are preserved. Returns {'refused': n} when n case(s) carry history: a reset of live data is a
    manual SQL step taken after a backup, never a button."""
    with conn.cursor() as cur:
        protected = _count_with_history(cur, "true")
        if protected:
            conn.rollback()
            return {"refused": protected}
        cur.execute("select count(*) from attendance.cases")
        n = cur.fetchone()[0]
        cur.execute("delete from attendance.cases")
        cur.execute("delete from attendance.ingestion_exceptions")
        cur.execute("delete from attendance.ingestion_runs")
    conn.commit()
    return {"cases_deleted": n}
