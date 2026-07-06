"""Contract for attachment validation, path building, and the storage client."""
import pytest

from app import storage


def test_validate_accepts_allowed_types():
    storage.validate_upload("application/pdf", 1000)
    storage.validate_upload("image/png", storage.MAX_BYTES)


def test_validate_rejects_bad_type_and_oversize():
    with pytest.raises(storage.StorageError):
        storage.validate_upload("text/html", 10)
    with pytest.raises(storage.StorageError):
        storage.validate_upload("application/pdf", storage.MAX_BYTES + 1)


def test_sanitize_strips_paths_and_unsafe_chars():
    assert storage.sanitize_filename("../../etc/passwd") == "passwd"
    assert storage.sanitize_filename("my file (1).pdf") == "my_file_1_.pdf"
    assert storage.sanitize_filename("") == "file"


def test_object_path_is_namespaced_by_case():
    p = storage.object_path("case-9", "cert.pdf")
    assert p.startswith("case-9/")
    assert p.endswith("_cert.pdf")


class _Resp:
    def __init__(self, code, payload=None, text=""):
        self.status_code = code
        self._p = payload or {}
        self.text = text

    def json(self):
        return self._p


class _Http:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def post(self, url, headers=None, data=None, json=None):
        self.calls.append({"url": url, "headers": headers, "data": data, "json": json})
        return self.resp


def test_signed_url_builds_full_url():
    http = _Http(_Resp(200, {"signedURL": "/object/sign/case-attachments/x?token=abc"}))
    c = storage.StorageClient("https://proj.supabase.co", "svc", http=http)
    url = c.signed_url("case-1/x.pdf")
    assert url == "https://proj.supabase.co/storage/v1/object/sign/case-attachments/x?token=abc"


def test_upload_raises_on_error_status():
    http = _Http(_Resp(403, text="denied"))
    c = storage.StorageClient("https://proj.supabase.co", "svc", http=http)
    with pytest.raises(storage.StorageError):
        c.upload("case-1/x.pdf", b"data", "application/pdf")
