"""Upload preview text (pure): shows resolved dates, periods and every warning; never cell text."""
import datetime

from app.ingest_flow import preview_lines

D = datetime.date


def _preview(**kw):
    base = {"first_date": D(2026, 12, 15), "last_date": D(2027, 1, 14), "days": 31,
            "periods": [(D(2026, 12, 15), D(2027, 1, 14))], "cells": 62, "cases": 3,
            "employees_in_sheet": 2, "exceptions": 1, "duplicate_dates": [], "duplicate_rows": 0,
            "mapped": (2, 2), "earlier_uploads": 0, "first_uploaded_at": None}
    return {**base, **kw}


def test_plain_preview_shows_range_and_period():
    text = "\n".join(preview_lines(_preview()))
    assert "Tue 15 Dec 2026" in text and "Thu 14 Jan 2027" in text and "31 date column(s)" in text
    assert "15 Dec 2026 → 14 Jan 2027" in text and "⚠️" not in text


def test_every_warning_is_shown():
    lines = preview_lines(_preview(
        duplicate_dates=[D(2026, 12, 20)], duplicate_rows=2, earlier_uploads=1,
        first_uploaded_at=datetime.datetime(2026, 12, 1, 9, 30), mapped=(None, None)))
    text = "\n".join(lines)
    assert "20 Dec 2026" in text and "2 repeated employee row(s)" in text
    assert "already uploaded 1 time(s) (first 2026-12-01 09:30)" in text
    assert "employees mapped" not in text
