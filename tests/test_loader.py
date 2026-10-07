"""Behavior contract for the DB loader orchestration (SPEC §6.1–6.2), DB injected as a fake."""
from ingestion import loader
from ingestion.reference import parse_reference
from ingestion.summary import ingest_summary


class FakeDB:
    def __init__(self, run_id="run-1"):
        self.calls = []
        self._run_id = run_id

    def one(self, sql, params):
        self.calls.append(("one", sql, params))
        return (self._run_id,)

    def many(self, sql, rows):
        self.calls.append(("many", sql, list(rows)))


def test_load_reference_upserts_managers_before_employees(sample_workbook_with_summary):
    ref = parse_reference(sample_workbook_with_summary)
    db = FakeDB()
    loader.load_reference(db, ref)

    assert [c[0] for c in db.calls] == ["many", "many"]
    assert db.calls[0][1] == loader.UPSERT_MANAGER
    assert len(db.calls[0][2]) == len(ref.managers)
    assert db.calls[1][1] == loader.UPSERT_EMPLOYEE
    assert len(db.calls[1][2]) == len(ref.employees)
    # employee params carry manager_crm (for the FK subselect), not a raw id
    e1 = next(p for p in db.calls[1][2] if p[0] == "E-1")
    assert e1[7] == "TL-A"


def test_upsert_manager_does_not_touch_link_token():
    # A plain re-ingest must preserve a manager's issued link: the manager upsert may not write
    # access_token or access_token_hash on conflict, or every re-ingest would invalidate links.
    assert "access_token" not in loader.UPSERT_MANAGER.lower()


def test_load_ingestion_records_run_then_cases_then_exceptions(sample_workbook_with_summary):
    ref = parse_reference(sample_workbook_with_summary)
    res = ingest_summary(sample_workbook_with_summary, ref, year=2026)
    db = FakeDB(run_id="run-xyz")
    summary = loader.load_ingestion(db, res, source_filename="wb.xlsx")

    kinds = [(c[0], c[1]) for c in db.calls]
    assert kinds[0] == ("one", loader.INSERT_RUN)
    assert kinds[1] == ("many", loader.AUDIT_SOURCE_CHANGE)   # before the upsert overwrites
    assert kinds[2] == ("many", loader.UPSERT_CASE)
    assert kinds[3] == ("many", loader.INSERT_EXCEPTION)

    assert summary.run_id == "run-xyz"
    assert summary.cases == len(res.cases)
    assert summary.exceptions == len(res.exceptions)
    # every exception row is linked to the run id
    assert all(row[0] == "run-xyz" for row in db.calls[3][2])
    # every case row carries the owning run id (last element)
    assert all(row[-1] == "run-xyz" for row in db.calls[2][2])


def test_reference_exceptions_are_persisted_too(sample_workbook_with_summary):
    ref = parse_reference(sample_workbook_with_summary)
    res = ingest_summary(sample_workbook_with_summary, ref, year=2026)
    db = FakeDB(run_id="r")
    summary = loader.load_ingestion(db, res, reference=ref)
    # combined ingestion + reference exceptions
    assert summary.exceptions == len(res.exceptions) + len(ref.exceptions)
    exc_rows = db.calls[3][2]
    assert len(exc_rows) == len(res.exceptions) + len(ref.exceptions)
    # reference exceptions (e.g. unmapped_employee) are present
    assert any(row[4] == "unmapped_employee" for row in exc_rows)


def test_case_params_shape(sample_workbook_with_summary):
    ref = parse_reference(sample_workbook_with_summary)
    res = ingest_summary(sample_workbook_with_summary, ref, year=2026)
    c = next(c for c in res.cases if c.employee_crm == "E-1")
    assert loader.case_params(c, "run-1") == ("E-1", "TL-A", c.work_date, "Absent", False, "run-1")


def test_upsert_case_does_not_reassign_owning_run_on_conflict():
    # Creator-owns: a re-ingest that re-touches a day must not steal the case's ingestion_run_id.
    assert "ingestion_run_id = excluded" not in loader.UPSERT_CASE.lower()


def test_source_change_audit_runs_for_every_case_with_new_values_and_actor(
        sample_workbook_with_summary):
    ref = parse_reference(sample_workbook_with_summary)
    res = ingest_summary(sample_workbook_with_summary, ref, year=2026)
    db = FakeDB(run_id="run-new")
    loader.load_ingestion(db, res, triggered_by="hrbp:hr@51talk.com")
    rows = db.calls[1][2]
    assert len(rows) == len(res.cases)
    c = res.cases[0]
    assert rows[0] == {"actor": "ingest:hrbp:hr@51talk.com", "employee_crm": c.employee_crm,
                       "work_date": c.work_date, "source_status": c.source_status,
                       "is_half_day": c.is_half_day, "run_id": "run-new"}


def test_source_change_audit_defaults_actor_to_system(sample_workbook_with_summary):
    ref = parse_reference(sample_workbook_with_summary)
    res = ingest_summary(sample_workbook_with_summary, ref, year=2026)
    db = FakeDB()
    loader.load_ingestion(db, res)
    assert {r["actor"] for r in db.calls[1][2]} == {"ingest:system"}


def test_source_change_audit_only_fires_on_a_real_change():
    # Old and new values both recorded; unchanged re-uploads write nothing. The behaviour itself
    # (one row per changed case, none otherwise) is asserted against Postgres in tests/pg/.
    sql = " ".join(loader.AUDIT_SOURCE_CHANGE.lower().split())
    assert "'source_status_changed'" in sql
    assert "is distinct from %(source_status)s::text" in sql
    assert "is distinct from %(is_half_day)s::boolean" in sql
    for key in ("employee_crm", "work_date", "source_status", "is_half_day"):
        assert sql.count(f"'{key}'") == 2              # in both the old and the new snapshot
    # old = the run that set the replaced value (latest change's new run, else the owning run);
    # new = this upload. The chain itself is proven against Postgres in tests/pg/.
    assert "coalesce( (select (l.new_value->>'ingestion_run_id')::uuid" in sql
    assert "'ingestion_run_id', %(run_id)s::uuid" in sql
