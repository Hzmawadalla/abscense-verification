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


def test_upsert_manager_does_not_touch_link_token():
    # A plain re-ingest must preserve a manager's issued link: the manager upsert may not write
    # access_token or access_token_hash on conflict, or every re-ingest would invalidate links.
    assert "access_token" not in loader.UPSERT_MANAGER.lower()
    e1 = next(p for p in db.calls[1][2] if p[0] == "E-1")
    assert e1[7] == "TL-A"


def test_load_ingestion_records_run_then_cases_then_exceptions(sample_workbook_with_summary):
    ref = parse_reference(sample_workbook_with_summary)
    res = ingest_summary(sample_workbook_with_summary, ref, year=2026)
    db = FakeDB(run_id="run-xyz")
    summary = loader.load_ingestion(db, res, source_filename="wb.xlsx")

    kinds = [(c[0], c[1]) for c in db.calls]
    assert kinds[0] == ("one", loader.INSERT_RUN)
    assert kinds[1] == ("many", loader.UPSERT_CASE)
    assert kinds[2] == ("many", loader.INSERT_EXCEPTION)

    assert summary.run_id == "run-xyz"
    assert summary.cases == len(res.cases)
    assert summary.exceptions == len(res.exceptions)
    # every exception row is linked to the run id
    assert all(row[0] == "run-xyz" for row in db.calls[2][2])


def test_reference_exceptions_are_persisted_too(sample_workbook_with_summary):
    ref = parse_reference(sample_workbook_with_summary)
    res = ingest_summary(sample_workbook_with_summary, ref, year=2026)
    db = FakeDB(run_id="r")
    summary = loader.load_ingestion(db, res, reference=ref)
    # combined ingestion + reference exceptions
    assert summary.exceptions == len(res.exceptions) + len(ref.exceptions)
    exc_rows = db.calls[2][2]
    assert len(exc_rows) == len(res.exceptions) + len(ref.exceptions)
    # reference exceptions (e.g. unmapped_employee) are present
    assert any(row[4] == "unmapped_employee" for row in exc_rows)


def test_case_params_shape(sample_workbook_with_summary):
    ref = parse_reference(sample_workbook_with_summary)
    res = ingest_summary(sample_workbook_with_summary, ref, year=2026)
    c = next(c for c in res.cases if c.employee_crm == "E-1")
    assert loader.case_params(c) == ("E-1", "TL-A", c.work_date, "Absent", False)
