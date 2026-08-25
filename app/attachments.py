"""Rendering decisions for TL-uploaded evidence.

Kept free of any Streamlit dependency so the grouping and content-type logic can be unit-tested
without booting the app (same split as `app.verdicts` / `app.security`).
"""
from __future__ import annotations

from collections import defaultdict


def group_by_case(rows: list[dict]) -> dict[int, list[dict]]:
    """Flat attachment rows -> {case_id: [row, ...]}, preserving the query's order.

    The dashboard fetches every case's attachments in one query; this turns that flat result
    into the per-case lookup the render loop needs.
    """
    grouped: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        grouped[r["case_id"]].append(r)
    return dict(grouped)


def is_image(content_type: str | None) -> bool:
    """Whether this attachment can be shown inline with st.image().

    Screenshots (png/jpeg) render in the page; anything else — PDFs, or rows stored before a
    content type was recorded — degrades to a download link rather than breaking the page.
    """
    return bool(content_type) and content_type.lower().startswith("image/")
