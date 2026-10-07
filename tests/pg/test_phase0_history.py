"""Phase 0 against real PostgreSQL: constraints, the history predicate, void, override, and the
re-upload source-status audit. These are the behaviours an in-memory fake cannot prove."""
import datetime
from pathlib import Path

import pytest
from psycopg.errors import ForeignKeyViolation, RaiseException
from psycopg.rows import dict_row

from app import data
from ingestion import loader
from ingestion.db_psycopg import PsycopgDB
from ingestion.summary import CaseCandidate, IngestionResult

DAY = datetime.date(2026, 9, 2)
HRBP = "hrbp:hr@51talk.com"


def _q(conn, sql, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.commit()
    return rows


def _answer(conn, cid, verdict="present", comment="worked", path="p/1.png"):
    data.add_attachment(conn, cid, path, path.split("/")[-1], "image/png", 10)
    assert data.submit_verdict(conn, cid, verdict, None, comment, "tl:TL-A")


# ------------------------------------------------------------------ migration / constraints
def test_attachment_and_audit_fks_are_restrict(conn):
    rows = _q(conn, "select conname, confdeltype from pg_constraint where conname in "
                    "('case_attachments_case_id_fkey', 'audit_log_case_id_fkey')")
    assert {r["conname"]: r["confdeltype"] for r in rows} == {
        "case_attachments_case_id_fkey": "r", "audit_log_case_id_fkey": "r"}


def test_case_with_attachment_cannot_be_deleted(conn, seed):
    cid = seed["new_case"](seed["new_run"](), DAY)
    data.add_attachment(conn, cid, "p/1.png", "1.png", "image/png", 10)
    with pytest.raises(ForeignKeyViolation), conn.cursor() as cur:
        cur.execute("delete from attendance.cases where id = %s", (cid,))
    conn.rollback()
    assert len(_q(conn, "select 1 from attendance.case_attachments")) == 1


def test_case_with_audit_entry_cannot_be_deleted(conn, seed):
    cid = seed["new_case"](seed["new_run"](), DAY)
    assert data.submit_verdict(conn, cid, "absent", None, None, "tl:TL-A")
    with pytest.raises(ForeignKeyViolation), conn.cursor() as cur:
        cur.execute("delete from attendance.cases where id = %s", (cid,))
    conn.rollback()


# ------------------------------------------------------------------ void keeps history
def test_void_keeps_answer_history_and_evidence(conn, seed):
    cid = seed["new_case"](seed["new_run"](), DAY)
    _answer(conn, cid)
    res = data.reopen_tl_cases(conn, [cid], HRBP, "wrong screenshot")
    assert res == {"reopened": 1, "skipped": 0, "attachments_voided": 1}

    case = _q(conn, "select status, manager_status, manager_comment from attendance.cases "
                    "where id = %s", (cid,))[0]
    assert case == {"status": "open", "manager_status": None, "manager_comment": None}
    att = _q(conn, "select storage_path, voided_at, voided_by from attendance.case_attachments "
                   "where case_id = %s", (cid,))
    assert len(att) == 1 and att[0]["voided_at"] is not None and att[0]["voided_by"] == HRBP

    audit = _q(conn, "select action, old_value, new_value from attendance.audit_log "
                     "where case_id = %s order by created_at", (cid,))
    assert [a["action"] for a in audit] == ["tl_verdict", "hrbp_void"]
    assert audit[0]["new_value"]["manager_comment"] == "worked"        # original answer kept
    old = audit[1]["old_value"]
    assert old["manager_status"] == "present" and old["manager_comment"] == "worked"
    assert old["manager_responded_at"]                                   # timestamp kept


def test_after_void_and_new_answer_only_new_evidence_is_current(conn, seed):
    cid = seed["new_case"](seed["new_run"](), DAY)
    _answer(conn, cid, path="p/old.png")
    data.reopen_tl_cases(conn, [cid], HRBP, "redo")
    _answer(conn, cid, verdict="present", comment="second try", path="p/new.png")

    current = data.attachments_for_cases(conn, [cid])
    assert [a["storage_path"] for a in current] == ["p/new.png"]
    assert [a["storage_path"] for a in data.list_attachments(conn, cid)] == ["p/new.png"]
    every = data.list_attachments(conn, cid, include_voided=True)
    assert [a["storage_path"] for a in every] == ["p/old.png", "p/new.png"]
    actions = [a["action"] for a in _q(conn, "select action from attendance.audit_log "
                                             "where case_id = %s order by created_at", (cid,))]
    assert actions == ["tl_verdict", "hrbp_void", "tl_verdict"]


# ------------------------------------------------------------------ upload deletion safety
def test_voided_case_blocks_upload_removal(conn, seed):
    run = seed["new_run"]()
    cid = seed["new_case"](run, DAY)
    _answer(conn, cid)
    data.reopen_tl_cases(conn, [cid], HRBP, "redo")         # open again, no live answer

    uploads = {u["id"]: u for u in data.list_uploads(conn)}
    assert uploads[run]["verified"] == 0 and uploads[run]["protected"] == 1
    assert data.remove_upload(conn, run) == {"refused": 1}
    assert len(_q(conn, "select 1 from attendance.cases")) == 1


def test_untouched_upload_can_still_be_removed(conn, seed):
    run = seed["new_run"]()
    seed["new_case"](run, DAY)
    seed["new_case"](run, DAY + datetime.timedelta(days=1))
    assert data.remove_upload(conn, run) == {"cases_deleted": 2}
    assert _q(conn, "select 1 from attendance.ingestion_runs") == []


def test_reset_all_refused_while_history_exists(conn, seed):
    run = seed["new_run"]()
    cid = seed["new_case"](run, DAY)
    seed["new_case"](run, DAY + datetime.timedelta(days=1))
    assert data.submit_verdict(conn, cid, "absent", None, None, "tl:TL-A")
    assert data.reset_all_cases(conn) == {"refused": 1}
    assert len(_q(conn, "select 1 from attendance.cases")) == 2


# ------------------------------------------------------------------ HRBP Override
def test_override_keeps_tl_comment_and_records_metadata(conn, seed):
    cid = seed["new_case"](seed["new_run"](), DAY)
    _answer(conn, cid, verdict="absent", comment="was on marriage leave")
    assert data.close_case(conn, cid, HRBP, final_status="leave", comment="marriage leave")
    row = _q(conn, "select manager_status, manager_comment, final_status, hrbp_override_note, "
                   "hrbp_override_by, hrbp_override_at from attendance.cases where id = %s",
             (cid,))[0]
    assert row["manager_status"] == "absent"
    assert row["manager_comment"] == "was on marriage leave"
    assert row["final_status"] == "leave"
    assert row["hrbp_override_note"] == "marriage leave" and row["hrbp_override_by"] == HRBP
    assert row["hrbp_override_at"] is not None
    listed = next(r for r in data.list_cases(conn, status="closed") if r["id"] == cid)
    assert listed["hrbp_override_note"] == "marriage leave"


# ------------------------------------------------------------------ re-upload source history
def _load(conn, status, run_name, half_day=False):
    res = IngestionResult(cases=[CaseCandidate("E-1", "TL-A", DAY, status, half_day)])
    with conn.transaction():
        return loader.load_ingestion(PsycopgDB(conn), res, source_filename=run_name,
                                     triggered_by=HRBP).run_id


def _source_audits(conn):
    return _q(conn, "select actor, old_value, new_value from attendance.audit_log "
                    "where action = 'source_status_changed' order by created_at")


def test_reupload_change_is_audited_with_old_and_new_values(conn, seed):
    run1 = _load(conn, "Absent", "week1.xlsx")
    run2 = _load(conn, "No Show", "week2.xlsx")
    audits = _source_audits(conn)
    assert len(audits) == 1
    a = audits[0]
    assert a["actor"] == f"ingest:{HRBP}"
    assert a["old_value"]["source_status"] == "Absent"
    assert a["new_value"]["source_status"] == "No Show"
    assert a["old_value"]["ingestion_run_id"] == str(run1)
    assert a["new_value"]["ingestion_run_id"] == str(run2)
    assert a["old_value"]["employee_crm"] == "E-1" and a["old_value"]["work_date"] == str(DAY)
    case = _q(conn, "select source_status, ingestion_run_id from attendance.cases")[0]
    assert case["source_status"] == "No Show"         # upsert behaviour itself unchanged
    assert case["ingestion_run_id"] == run1           # creator-owns, as before


def test_three_uploads_chain_previous_upload_correctly(conn, seed):
    run1 = _load(conn, "Absent", "week1.xlsx")
    run2 = _load(conn, "No Show", "week2.xlsx")
    run3 = _load(conn, "Absent", "week3.xlsx")
    chain = [(a["old_value"]["ingestion_run_id"], a["new_value"]["ingestion_run_id"],
              a["old_value"]["source_status"], a["new_value"]["source_status"])
             for a in _source_audits(conn)]
    assert chain == [(str(run1), str(run2), "Absent", "No Show"),
                     (str(run2), str(run3), "No Show", "Absent")]   # run2 -> run3, not run1
    case = _q(conn, "select ingestion_run_id from attendance.cases")[0]
    assert case["ingestion_run_id"] == run1           # owning run still never reassigned


def test_unchanged_upload_between_changes_does_not_break_the_chain(conn, seed):
    run1 = _load(conn, "Absent", "week1.xlsx")
    run2 = _load(conn, "No Show", "week2.xlsx")
    _load(conn, "No Show", "week3.xlsx")              # same value: no audit, no new link
    run4 = _load(conn, "Absent", "week4.xlsx")
    chain = [(a["old_value"]["ingestion_run_id"], a["new_value"]["ingestion_run_id"])
             for a in _source_audits(conn)]
    assert chain == [(str(run1), str(run2)), (str(run2), str(run4))]


def test_identical_reupload_writes_no_audit(conn, seed):
    _load(conn, "Absent", "week1.xlsx")
    _load(conn, "Absent", "week1-again.xlsx")
    assert _source_audits(conn) == []


def test_first_upload_writes_no_source_audit(conn, seed):
    _load(conn, "Absent", "week1.xlsx")
    assert _source_audits(conn) == []
    assert len(_q(conn, "select 1 from attendance.cases")) == 1


def test_half_day_flag_change_is_audited(conn, seed):
    _load(conn, "Absent", "week1.xlsx")
    _load(conn, "Absent", "week2.xlsx", half_day=True)
    audits = _source_audits(conn)
    assert len(audits) == 1
    assert audits[0]["old_value"]["is_half_day"] is False
    assert audits[0]["new_value"]["is_half_day"] is True


# ------------------------------------------------------------------ unchanged behaviour
def test_unanswered_case_submit_is_still_one_time(conn, seed):
    cid = seed["new_case"](seed["new_run"](), DAY)
    assert data.submit_verdict(conn, cid, "absent", None, None, "tl:TL-A")
    assert not data.submit_verdict(conn, cid, "present", None, None, "tl:TL-A")


# ------------------------------------------------------------------ migration rollback SQL
MIGRATION = next((Path(__file__).resolve().parents[2] / "supabase" / "migrations")
                 .glob("*_phase0_preserve_history.sql"))


def _body(sql_lines):
    """Statements without the file's own begin/commit, so the test controls the transaction."""
    keep = [ln for ln in sql_lines if ln.strip().lower() not in ("begin;", "commit;")]
    return "\n".join(keep)


def _fk_rules(conn):
    with conn.cursor() as cur:
        cur.execute("select conname, confdeltype from pg_constraint where conname in "
                    "('case_attachments_case_id_fkey', 'audit_log_case_id_fkey')")
        return dict(cur.fetchall())


def _has_column(conn, table, column):
    with conn.cursor() as cur:
        cur.execute("select 1 from information_schema.columns where table_schema = 'attendance' "
                    "and table_name = %s and column_name = %s", (table, column))
        return cur.fetchone() is not None


def _rollback_body():
    text = MIGRATION.read_text(encoding="utf-8").splitlines()
    marker = next(i for i, ln in enumerate(text) if ln.startswith("-- ROLLBACK"))
    tail = [ln[3:] if ln.startswith("-- ") else "" for ln in text[marker:]]
    return "\n".join(tail[tail.index("begin;") + 1:tail.index("commit;")]), _body(text[:marker])


def test_rollback_sql_restores_previous_schema_and_migration_reapplies(conn):
    rollback, forward = _rollback_body()            # exactly the commented undo block
    with conn.cursor() as cur:
        cur.execute(rollback)
        assert _fk_rules(conn) == {"case_attachments_case_id_fkey": "c",
                                   "audit_log_case_id_fkey": "n"}
        assert not _has_column(conn, "case_attachments", "voided_at")
        assert not _has_column(conn, "cases", "hrbp_override_note")
        cur.execute(forward)                     # and the migration applies cleanly again
        assert _fk_rules(conn) == {"case_attachments_case_id_fkey": "r",
                                   "audit_log_case_id_fkey": "r"}
        assert _has_column(conn, "cases", "hrbp_override_at")
    conn.rollback()                              # DDL is transactional: leave the cluster as-is


def test_migration_refuses_a_renamed_fk_and_changes_nothing(conn):
    rollback, forward = _rollback_body()
    with conn.cursor() as cur:
        cur.execute(rollback)                                  # back to the pre-Phase-0 schema
        cur.execute("alter table attendance.case_attachments "
                    "rename constraint case_attachments_case_id_fkey to legacy_attachments_fk")
        cur.execute("savepoint before_migration")
        with pytest.raises(RaiseException, match="precondition failed on attendance.case_attachments"):
            cur.execute(forward)
        cur.execute("rollback to savepoint before_migration")
        # the old CASCADE rule is still the only FK — nothing half-applied
        cur.execute("select conname, confdeltype from pg_constraint "
                    "where conrelid = 'attendance.case_attachments'::regclass and contype = 'f'")
        assert cur.fetchall() == [("legacy_attachments_fk", "c")]
        assert not _has_column(conn, "case_attachments", "voided_at")
    conn.rollback()


def test_migration_refuses_a_duplicate_fk(conn):
    rollback, forward = _rollback_body()
    with conn.cursor() as cur:
        cur.execute(rollback)
        cur.execute("alter table attendance.audit_log add constraint extra_audit_fk "
                    "foreign key (case_id) references attendance.cases(id) on delete cascade")
        with pytest.raises(RaiseException, match="precondition failed on attendance.audit_log"):
            cur.execute(forward)
    conn.rollback()


def test_migration_refuses_an_unexpected_delete_rule(conn):
    rollback, forward = _rollback_body()
    with conn.cursor() as cur:
        cur.execute(rollback)
        cur.execute("alter table attendance.case_attachments drop constraint "
                    "case_attachments_case_id_fkey; alter table attendance.case_attachments "
                    "add constraint case_attachments_case_id_fkey foreign key (case_id) "
                    "references attendance.cases(id) on delete set null")
        with pytest.raises(RaiseException, match="confdeltype n"):
            cur.execute(forward)
    conn.rollback()


def test_migration_refuses_to_run_twice(conn):
    _, forward = _rollback_body()
    with conn.cursor() as cur, pytest.raises(RaiseException, match="precondition failed"):
        cur.execute(forward)                                   # already applied: rules are RESTRICT
    conn.rollback()


def test_after_migration_exactly_one_restrict_fk_per_column(conn):
    with conn.cursor() as cur:
        cur.execute("select con.conrelid::regclass::text, count(*), "
                    "       string_agg(con.confdeltype::text, '') "
                    "from pg_constraint con join pg_attribute att "
                    "  on att.attrelid = con.conrelid and att.attnum = any(con.conkey) "
                    "where con.contype = 'f' and att.attname = 'case_id' and con.conrelid in "
                    "  ('attendance.case_attachments'::regclass, 'attendance.audit_log'::regclass) "
                    "group by 1 order by 1")
        assert cur.fetchall() == [("attendance.audit_log", 1, "r"),
                                  ("attendance.case_attachments", 1, "r")]
    conn.rollback()


def _swap_attachments_fk(cur, definition):
    """Replace case_attachments' FK with a correctly NAMED, CASCADE one defined by `definition`."""
    cur.execute("alter table attendance.case_attachments "
                "drop constraint case_attachments_case_id_fkey")
    cur.execute("alter table attendance.case_attachments add constraint "
                f"case_attachments_case_id_fkey {definition} on delete cascade")


def _assert_refused_and_unchanged(conn, cur, forward):
    cur.execute("savepoint before_migration")
    with pytest.raises(RaiseException, match="precondition failed on attendance.case_attachments"):
        cur.execute(forward)
    cur.execute("rollback to savepoint before_migration")
    assert not _has_column(conn, "case_attachments", "voided_at")      # nothing applied
    assert _fk_rules(conn)["case_attachments_case_id_fkey"] == "c"     # old rule still there


def test_migration_refuses_fk_to_the_wrong_table(conn):
    rollback, forward = _rollback_body()
    with conn.cursor() as cur:
        cur.execute(rollback)
        cur.execute("create table attendance.cases_shadow (id uuid primary key)")
        _swap_attachments_fk(cur, "foreign key (case_id) references attendance.cases_shadow(id)")
        _assert_refused_and_unchanged(conn, cur, forward)
    conn.rollback()


def test_migration_refuses_fk_to_the_wrong_column(conn):
    rollback, forward = _rollback_body()
    with conn.cursor() as cur:
        cur.execute(rollback)
        cur.execute("alter table attendance.cases add column alt_id uuid unique")
        _swap_attachments_fk(cur, "foreign key (case_id) references attendance.cases(alt_id)")
        _assert_refused_and_unchanged(conn, cur, forward)
    conn.rollback()


def test_migration_refuses_a_composite_fk(conn):
    rollback, forward = _rollback_body()
    with conn.cursor() as cur:
        cur.execute(rollback)
        cur.execute("alter table attendance.cases add constraint cases_id_work_date_uniq "
                    "unique (id, work_date)")
        cur.execute("alter table attendance.case_attachments add column work_date date")
        _swap_attachments_fk(cur, "foreign key (case_id, work_date) "
                                  "references attendance.cases(id, work_date)")
        _assert_refused_and_unchanged(conn, cur, forward)
    conn.rollback()
