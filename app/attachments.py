"""Rendering decisions for TL-uploaded evidence.

Kept free of any Streamlit dependency so the grouping and content-type logic can be unit-tested
without booting the app (same split as `app.verdicts` / `app.security`).
"""
from __future__ import annotations

from collections import defaultdict
from uuid import UUID


def group_by_case(rows: list[dict]) -> dict[UUID, list[dict]]:
    """Flat attachment rows -> {case_id: [row, ...]}, preserving the query's order.

    The dashboard fetches every case's attachments in one query; this turns that flat result
    into the per-case lookup the render loop needs.
    """
    grouped: dict[UUID, list[dict]] = defaultdict(list)
    for r in rows:
        grouped[r["case_id"]].append(r)
    return dict(grouped)


# Signed links inside the dashboard table are short-lived (the StorageClient default). Links baked
# into a downloaded list get a longer life so the file is still usable the next working day —
# anyone holding that file can open the screenshots for this long without logging in.
EXPORT_EXPIRES_IN = 7 * 24 * 3600


def filter_by_crm(rows: list[dict], query: str) -> list[dict]:
    """Case rows whose employee CRM contains `query`, case-insensitively.

    A blank query means "no filter". Rows with no CRM on file never match a non-blank query
    rather than raising, so one incomplete record can't break the dashboard.
    """
    q = (query or "").strip().lower()
    if not q:
        return rows
    return [r for r in rows if q in (r.get("employee_crm") or "").lower()]


def evidence_columns(case_rows: list[dict], by_case: dict[UUID, list[dict]],
                     links: dict[str, str]) -> list[dict]:
    """Copy of `case_rows` with two columns added: `evidence` (file count) and `screenshot` (link).

    The count comes from one already-fetched query, so it is free and always shown. The link is
    only filled for storage paths present in `links` — signing is an HTTP round-trip per file, so
    the caller signs on demand and passes the results in. Where a case holds several files the
    link points at the first; the count reveals that more exist.

    Returns new dicts; the input rows are left untouched.
    """
    out = []
    for r in case_rows:
        files = by_case.get(r["id"], [])
        first = files[0]["storage_path"] if files else None
        out.append({**r, "evidence": len(files),
                    "screenshot": links.get(first) if first else None})
    return out
