"""Attach an ORIGINAL attendance workbook's cells to its EXISTING legacy upload (see
ingestion/backfill.py for exactly what is and is not written).

Dry run (the default) is read-only: the session is forced read-only and the checks run inside
BEGIN READ ONLY ... ROLLBACK. It prints counts only — never names, CRMs or cell values.

  SUPABASE_DB_URL=postgresql://... python tools/backfill_cells.py --run-id <uuid> --year 2026 FILE
  SUPABASE_DB_URL=postgresql://... python tools/backfill_cells.py --run-id <uuid> --year 2026 FILE \\
      --apply --expect-sha256 <sha256 printed by the dry run> [--expect-project-ref <ref>]

--apply writes ONE transaction for ONE upload, and only when --expect-sha256 equals the hash of the
bytes read (once) from FILE; those same bytes are parsed and written. The connection string is read
from the environment only; --expect-project-ref (optional) refuses unless the connection targets
that Supabase project. Errors are reported as one sanitized line (no connection details).
"""
import argparse
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from ingestion.backfill import (BackfillRefused, apply_backfill, plan_backfill,  # noqa: E402
                                sha256_of)

DESCRIPTION = __doc__.split("\n\n")[0].replace("\n", " ")


def _connect(dsn):
    import psycopg
    return psycopg.connect(dsn, autocommit=True, prepare_threshold=None, connect_timeout=15)


def _targets_project(dsn, ref) -> bool:
    u = urlsplit(dsn)
    return ref in unquote(u.username or "") or ref in (u.hostname or "")


def _db_error(e) -> str:
    """One line without the error text (which can carry host or user names)."""
    return (f"ERROR: database error ({type(e).__name__}, SQLSTATE {getattr(e, 'sqlstate', None) or '-'})."
            " Nothing was written.")


def run(dsn, run_id, path, year, apply=False, expect_sha256=None, out=print,
        expect_project_ref=None) -> int:
    import psycopg
    try:
        run_id = str(uuid.UUID(str(run_id)))
    except ValueError:
        out("REFUSED: --run-id is not a valid upload id. Nothing was written.")
        return 2
    if expect_project_ref and not _targets_project(dsn, expect_project_ref):
        out("REFUSED: the connection does not target the expected project. Nothing was written.")
        return 2
    path = Path(path)
    try:
        data = path.read_bytes()        # read ONCE: hash, parse and written cells all use these bytes
    except OSError as e:
        out(f"ERROR: the workbook file could not be read ({type(e).__name__}). Nothing was written.")
        return 3
    sha = sha256_of(data)
    if apply and expect_sha256 != sha:
        out("REFUSED: --apply needs --expect-sha256 equal to this file's sha256 "
            "(run the dry run first). Nothing was written.")
        return 2
    try:
        conn = _connect(dsn)
    except psycopg.Error as e:
        out(_db_error(e))
        return 3
    try:
        if not apply:
            conn.execute("set default_transaction_read_only = on")
            conn.execute("begin read only")
        try:
            plan = plan_backfill(conn, run_id, data, path.name, year)
        except BackfillRefused as e:
            out(f"REFUSED: {e}. Nothing was written.")
            return 2
        finally:
            if not apply:
                conn.execute("rollback")
        out(f"upload {plan.run_id}: workbook verified against {plan.cases_checked} case(s); "
            f"{len(plan.cells)} cell(s) for {plan.distinct_crms} CRM(s) x {plan.distinct_dates} "
            f"date(s), {plan.range_start} -> {plan.range_end}; sha256 {sha}")
        outside = ", ".join(str(d) for d in plan.dates_outside_evidence) or "none"
        out(f"evidence: {plan.exceptions_matched} flagged cell(s) matched to this upload's "
            f"exceptions, {plan.overlaps_supported} to earlier uploads' cases; dates outside the "
            f"case/exception span (unflagged, reported not refused): {outside}")
        if not apply:
            out("DRY RUN — nothing was written.")
            return 0
        try:
            written = apply_backfill(conn, plan, expect_sha256)
        except BackfillRefused as e:
            out(f"REFUSED: {e}. Nothing was written.")
            return 2
        out(f"WRITTEN: {written} cell(s) attached to upload {plan.run_id}; full_cells = true.")
        return 0
    except psycopg.Error as e:
        out(_db_error(e))
        return 3
    finally:
        conn.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=DESCRIPTION)
    ap.add_argument("file", help="the original workbook (.xlsx)")
    ap.add_argument("--run-id", required=True, help="the existing upload's id (uuid)")
    ap.add_argument("--year", type=int, required=True, help="year of the FIRST date column")
    ap.add_argument("--apply", action="store_true", help="write (default: read-only dry run)")
    ap.add_argument("--expect-sha256", help="required with --apply: the hash from the dry run")
    ap.add_argument("--expect-project-ref", help="optional: refuse unless the connection targets "
                                                 "this Supabase project ref")
    a = ap.parse_args(argv)
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        sys.exit("SUPABASE_DB_URL is not set (environment only).")
    try:
        code = run(dsn, a.run_id, a.file, a.year, a.apply, a.expect_sha256,
                   expect_project_ref=a.expect_project_ref)
    except Exception as e:  # noqa: BLE001 — never a raw traceback (could carry connection details)
        print(f"ERROR: unexpected failure ({type(e).__name__}). Nothing was written unless "
              "WRITTEN was printed.")
        code = 3
    sys.exit(code)


if __name__ == "__main__":
    main()
