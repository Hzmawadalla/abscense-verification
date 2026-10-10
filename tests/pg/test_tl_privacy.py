"""TL privacy at the data boundary, against real PostgreSQL: the TL page's query returns approved
labels only — never raw source text, leave type or another TL's comment — while HRBP keeps every
original detail."""
import datetime

from app import data
from app.tl_labels import FLAGGED, LEAVE, TL_LABELS

D = datetime.date
SECRET_WORDS = ("Hospital", "Surgery", "Bereavement", "Annual", "Failed", "previous TL",
                "To Be Confirmed", "sick_leave", "deducted")


def _case(conn, seed, day, source, status="open", manager_status=None, comment=None,
          leave_type=None, verdict_actor=None):
    run = seed["new_run"]()
    cid = seed["new_case"](run, day, source)
    with conn.cursor() as cur:
        cur.execute("update attendance.cases set status = %s, manager_status = %s, "
                    "manager_comment = %s, leave_type = %s where id = %s",
                    (status, manager_status, comment, leave_type, cid))
        if verdict_actor:
            cur.execute("insert into attendance.audit_log (case_id, actor, action) "
                        "values (%s, %s, 'tl_verdict')", (cid, verdict_actor))
    conn.commit()
    return cid


def _mgr(conn, seed):
    with conn.cursor() as cur:
        cur.execute("select id, crm from attendance.managers where id = %s", (seed["manager_id"],))
        mid, crm = cur.fetchone()
    return {"id": mid, "crm": crm}


def test_tl_rows_carry_labels_only_and_hrbp_keeps_the_original(conn, seed):
    mgr = _mgr(conn, seed)
    _case(conn, seed, D(2026, 10, 1), "Hospital visit - check")
    _case(conn, seed, D(2026, 10, 2), "Surgery (Pending)")
    _case(conn, seed, D(2026, 10, 3), "Bereavement Leave - To Be Confirmed")
    _case(conn, seed, D(2026, 10, 4), "Absent")
    own = _case(conn, seed, D(2026, 10, 5), "Absent (HD)", status="manager_responded",
                manager_status="absent", comment="called twice, no answer",
                verdict_actor=data.tl_actor(mgr))
    _case(conn, seed, D(2026, 10, 6), "Annual Leave (Failed)", status="manager_responded",
          manager_status="sick_leave", comment="previous TL private note", leave_type="sick_leave",
          verdict_actor="tl:TL-OLD")

    rows = data.tl_cases_for_manager(conn, mgr)

    assert len(rows) == 6
    for r in rows:
        assert tuple(r) == data.TL_CASE_FIELDS                    # whitelist, exact order
        assert r["status_label"] in TL_LABELS
        assert r["verdict_label"] is None or r["verdict_label"] in TL_LABELS
    by_day = {r["work_date"].day: r for r in rows}
    assert [by_day[d]["status_label"] for d in range(1, 7)] == [
        FLAGGED, FLAGGED, LEAVE, "Absent", "Absent", LEAVE]
    assert by_day[5]["own_comment"] == "called twice, no answer" and by_day[5]["id"] == own
    assert by_day[6]["own_comment"] is None and by_day[6]["verdict_label"] == LEAVE
    text = repr(rows)
    for word in SECRET_WORDS:
        assert word not in text

    # HRBP-facing data is untouched and exact
    with conn.cursor() as cur:
        cur.execute("select source_status, manager_comment, leave_type from attendance.cases "
                    "where work_date = %s", (D(2026, 10, 6),))
        assert cur.fetchone() == ("Annual Leave (Failed)", "previous TL private note", "sick_leave")


def test_another_tls_cases_are_never_returned(conn, seed):
    mgr = _mgr(conn, seed)
    _case(conn, seed, D(2026, 10, 1), "Absent")
    with conn.cursor() as cur:
        cur.execute("insert into attendance.managers (crm, name) values ('TL-Z', 'Other') "
                    "returning id")
        other = cur.fetchone()[0]
    conn.commit()
    assert data.tl_cases_for_manager(conn, {"id": other, "crm": "TL-Z"}) == []
    assert len(data.tl_cases_for_manager(conn, mgr)) == 1


def test_hrbp_void_list_still_shows_the_exact_source_text(conn, seed):
    cid = _case(conn, seed, D(2026, 10, 7), "Hospital visit - check", status="closed",
                manager_status="absent", comment="tl note")
    with conn.cursor() as cur:
        cur.execute("update attendance.cases set closed_by = 'tl' where id = %s", (cid,))
    conn.commit()
    rows = data.list_tl_submitted_cases(conn, seed["manager_id"])
    assert [(r["source_status"], r["manager_comment"]) for r in rows] == [
        ("Hospital visit - check", "tl note")]
