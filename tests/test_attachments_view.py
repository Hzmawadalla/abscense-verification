"""HRBP evidence view: batch-fetch TL attachments and decide how to render each one."""
from app import data
from app.attachments import group_by_case, is_image


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


def test_screenshots_render_as_images():
    assert is_image("image/png") is True
    assert is_image("image/jpeg") is True


def test_pdfs_do_not_render_as_images():
    assert is_image("application/pdf") is False


def test_missing_content_type_is_not_an_image():
    # Storage rows predating a content type must degrade to a link, never crash the page.
    assert is_image(None) is False
    assert is_image("") is False
