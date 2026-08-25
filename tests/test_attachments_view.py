"""HRBP evidence view: batch-fetch TL attachments and decide how to render each one."""
from app import data
from app.attachments import group_by_case


class FakeCursor:
    """psycopg-like cursor over an in-memory list of attachment rows. Only models the one
    query attachments_for_cases issues."""

    def __init__(self, rows, calls):
        self.rows, self.calls = rows, calls
        self._all = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.calls.append(" ".join(sql.lower().split()))
        wanted = set(params[0]) if params else set()
        self._all = [r for r in self.rows if r["case_id"] in wanted]

    def fetchall(self):
        return self._all


class FakeConn:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def cursor(self, row_factory=None):
        return FakeCursor(self.rows, self.calls)


ROWS = [
    {"case_id": 1, "storage_path": "c1/a.png", "filename": "a.png", "content_type": "image/png"},
    {"case_id": 1, "storage_path": "c1/b.pdf", "filename": "b.pdf", "content_type": "application/pdf"},
    {"case_id": 2, "storage_path": "c2/c.jpg", "filename": "c.jpg", "content_type": "image/jpeg"},
]


def test_no_case_ids_skips_the_database_entirely():
    conn = FakeConn(ROWS)
    assert data.attachments_for_cases(conn, []) == []
    assert conn.calls == []


def test_every_case_is_fetched_in_a_single_query():
    # The dashboard renders many cases at once; one query per case would be an N+1.
    conn = FakeConn(ROWS)
    got = data.attachments_for_cases(conn, [1, 2])
    assert len(conn.calls) == 1
    assert len(got) == 3


def test_only_the_requested_cases_come_back():
    conn = FakeConn(ROWS)
    got = data.attachments_for_cases(conn, [2])
    assert [r["filename"] for r in got] == ["c.jpg"]


def test_group_by_case_keeps_every_file_under_its_case():
    assert group_by_case(ROWS) == {1: [ROWS[0], ROWS[1]], 2: [ROWS[2]]}


def test_group_by_case_of_nothing_is_empty():
    assert group_by_case([]) == {}



# --------------------------------------------------------------- dashboard evidence columns
from app.attachments import EXPORT_EXPIRES_IN, evidence_columns, filter_by_crm  # noqa: E402

CASES = [
    {"id": 1, "employee_name": "Alpha", "employee_crm": "EGLP-alpha"},
    {"id": 2, "employee_name": "Beta", "employee_crm": "EGLP-beta"},
    {"id": 3, "employee_name": "Gamma", "employee_crm": "EGLP-gamma"},
]


def test_export_links_last_seven_days():
    assert EXPORT_EXPIRES_IN == 7 * 24 * 3600


def test_crm_search_is_case_insensitive_substring():
    assert [r["id"] for r in filter_by_crm(CASES, "ALPHA")] == [1]
    assert [r["id"] for r in filter_by_crm(CASES, "eglp")] == [1, 2, 3]


def test_blank_crm_search_returns_everything():
    assert filter_by_crm(CASES, "") == CASES
    assert filter_by_crm(CASES, "   ") == CASES


def test_crm_search_ignores_surrounding_whitespace():
    assert [r["id"] for r in filter_by_crm(CASES, "  beta  ")] == [2]


def test_crm_search_tolerates_a_missing_crm():
    # A case whose employee has no CRM on file must not crash the dashboard.
    assert filter_by_crm([{"id": 9, "employee_crm": None}], "x") == []


def test_evidence_count_shows_without_any_signing():
    # Counts must be free: signing 800+ rows on page load would make the dashboard unusable.
    out = evidence_columns(CASES, {1: [ROWS[0], ROWS[1]], 2: [ROWS[2]]}, links={})
    assert [r["evidence"] for r in out] == [2, 1, 0]
    assert [r["screenshot"] for r in out] == [None, None, None]


def test_prepared_links_land_next_to_their_case():
    out = evidence_columns(CASES, {1: [ROWS[0]], 2: [ROWS[2]]},
                           links={"c1/a.png": "https://signed/a", "c2/c.jpg": "https://signed/c"})
    assert out[0]["screenshot"] == "https://signed/a"
    assert out[1]["screenshot"] == "https://signed/c"
    assert out[2]["screenshot"] is None


def test_evidence_columns_never_mutates_the_case_rows():
    # The dashboard reuses list_cases() output elsewhere; enriching must return new dicts.
    original = [{"id": 1, "employee_crm": "X"}]
    evidence_columns(original, {1: [ROWS[0]]}, links={"c1/a.png": "u"})
    assert original == [{"id": 1, "employee_crm": "X"}]
