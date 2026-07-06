"""Supabase Storage for case attachments (SPEC §7).

Private bucket; uploads validated (type + size); HRBP views via short-lived signed URLs served as
downloads (never inline → no stored-XSS against the HRBP session). Uses the service key server-side
only. HTTP transport is injectable so validation/path logic is unit-testable without network."""
import re
import uuid

BUCKET = "case-attachments"
MAX_BYTES = 10 * 1024 * 1024
ALLOWED = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png"}


class StorageError(RuntimeError):
    pass


def validate_upload(content_type: str, size: int) -> None:
    if content_type not in ALLOWED:
        raise StorageError(f"file type not allowed: {content_type!r} (pdf/jpg/png only)")
    if size > MAX_BYTES:
        raise StorageError(f"file too large: {size} bytes (max {MAX_BYTES})")


def sanitize_filename(name: str) -> str:
    base = re.split(r"[\\/]", name or "")[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._") or "file"
    return cleaned[:120]


def object_path(case_id, filename: str) -> str:
    return f"{case_id}/{uuid.uuid4().hex}_{sanitize_filename(filename)}"


class StorageClient:
    def __init__(self, url, service_key, bucket=BUCKET, http=None):
        self.url = url.rstrip("/")
        self.key = service_key
        self.bucket = bucket
        self._http = http

    def _session(self):
        if self._http is None:
            import requests
            self._http = requests
        return self._http

    def _auth(self):
        return {"Authorization": f"Bearer {self.key}"}

    def upload(self, path: str, data: bytes, content_type: str) -> str:
        r = self._session().post(
            f"{self.url}/storage/v1/object/{self.bucket}/{path}",
            headers={**self._auth(), "Content-Type": content_type, "x-upsert": "true"},
            data=data)
        if getattr(r, "status_code", 200) >= 300:
            raise StorageError(f"upload failed ({r.status_code}): {r.text}")
        return path

    def signed_url(self, path: str, expires_in: int = 3600) -> str:
        r = self._session().post(
            f"{self.url}/storage/v1/object/sign/{self.bucket}/{path}",
            headers={**self._auth(), "Content-Type": "application/json"},
            json={"expiresIn": expires_in})
        if getattr(r, "status_code", 200) >= 300:
            raise StorageError(f"sign failed ({r.status_code}): {r.text}")
        return f"{self.url}/storage/v1{r.json()['signedURL']}"
