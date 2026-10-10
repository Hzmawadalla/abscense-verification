"""HRBP upload: parse -> preview -> explicit confirmation -> load (Phase 1, M2).

Parsing never writes. The preview shows how the sheet's dates were resolved (the Year input is the
year of the FIRST date column), which 15th -> 14th Attendance Periods it touches, and what would be
stored, so a wrong year is seen BEFORE anything is loaded. Loading happens only on confirmation and
in one transaction: it either stores everything or nothing.

Kept free of Streamlit so the flow is testable (same split as app.verdicts / app.report).
"""
import hashlib

from ingestion import loader
from ingestion.db_psycopg import PsycopgDB
from ingestion.periods import attendance_period


def preview_key(ref_bytes, att_bytes, year) -> tuple:
    """Identity of one parse: a preview is valid only while the files and the year are unchanged."""
    digest = lambda b: hashlib.sha256(b).hexdigest() if b is not None else None  # noqa: E731
    return digest(ref_bytes), digest(att_bytes), int(year)


def build_preview(res, ref, earlier_uploads=()) -> dict:
    """What HRBP confirms: resolved range, periods, counts and warnings. No cell contents."""
    first, last = res.stats["date_range"]
    dates = sorted({c.work_date for c in res.cells}) or [first, last]
    periods = sorted({attendance_period(d) for d in dates})
    reasons = [e.reason for e in res.exceptions]
    return {
        "first_date": first, "last_date": last, "days": len(dates),
        "periods": periods,
        "cells": len(res.cells), "cases": len(res.cases),
        "employees_in_sheet": len({c.crm_key for c in res.cells}),
        "exceptions": len(res.exceptions),
        "duplicate_dates": list(res.stats.get("duplicate_dates", [])),
        "duplicate_rows": reasons.count("duplicate_row"),
        "mapped": (ref.stats.get("mapped_employees"), ref.stats.get("employees")),
        "earlier_uploads": len(earlier_uploads),
        "first_uploaded_at": earlier_uploads[0]["created_at"] if earlier_uploads else None,
    }


def preview_lines(p) -> list[str]:
    """The preview as markdown bullet lines, for the HRBP page."""
    span = (f"**{p['first_date']:%a %d %b %Y}** → **{p['last_date']:%a %d %b %Y}** "
            f"({p['days']} date column(s))")
    periods = ", ".join(f"{s:%d %b %Y} → {e:%d %b %Y}" for s, e in p["periods"])
    lines = [f"Resolved dates: {span}",
             f"Attendance period(s) (15th → 14th): {periods}",
             f"Will store {p['cells']} attendance cell(s) for {p['employees_in_sheet']} employee(s), "
             f"create/update {p['cases']} case(s), record {p['exceptions']} exception(s)."]
    mapped, total = p["mapped"]
    if mapped is not None:
        lines.append(f"{mapped}/{total} employees mapped to a TL in the reference workbook.")
    if p["duplicate_dates"]:
        lines.append("⚠️ Repeated date column(s) with identical values — the first column is used: "
                     + ", ".join(f"{d:%d %b %Y}" for d in p["duplicate_dates"]))
    if p["duplicate_rows"]:
        lines.append(f"⚠️ {p['duplicate_rows']} repeated employee row(s) — the first row of each "
                     "CRM is used; the repeats are listed in Exceptions.")
    if p["earlier_uploads"]:
        lines.append(f"⚠️ This exact file was already uploaded {p['earlier_uploads']} time(s) "
                     f"(first {p['first_uploaded_at']:%Y-%m-%d %H:%M}). Loading it again makes it "
                     "the newest source; its values are the same.")
    return lines


def load_confirmed(conn, ref, res, source_filename, actor, file_sha256):
    """Store the confirmed parse atomically: reference, run, cases, exceptions and every cell, or
    nothing (any error rolls the whole transaction back)."""
    first, last = res.stats["date_range"]
    with conn.transaction():  # commits on success WITHOUT closing the pooled connection
        db = PsycopgDB(conn)
        loader.load_reference(db, ref)
        return loader.load_ingestion(db, res, reference=ref, source_filename=source_filename,
                                     triggered_by=actor, range_start=first, range_end=last,
                                     file_sha256=file_sha256)
