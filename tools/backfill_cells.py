"""Attach an ORIGINAL attendance workbook's cells to its EXISTING legacy upload (see
ingestion/backfill.py for exactly what is and is not written).

Dry run (the default) is read-only: the session is forced read-only and the checks run inside
BEGIN READ ONLY ... ROLLBACK. It prints counts only — never names, CRMs or cell values.

  SUPABASE_DB_URL=postgresql://... python tools/backfill_cells.py --run-id <uuid> --year 2026 FILE
  SUPABASE_DB_URL=postgresql://... python tools/backfill_cells.py --run-id <uuid> --year 2026 FILE \\
      --apply --expect-sha256 <sha256 printed by the dry run>

--apply writes ONE transaction for ONE upload, and only when --expect-sha256 equals the file's hash
(so exactly the reviewed file is written). The connection string is read from the environment only.
"""
import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from ingestion.backfill import (BackfillRefused, apply_backfill, plan_backfill,  # noqa: E402
                                sha256_of)


def _connect(dsn):
    import psycopg
    return psycopg.connect(dsn, autocommit=True, prepare_threshold=None, connect_timeout=15)


def run(dsn, run_id, path, year, apply=False, expect_sha256=None, out=print) -> int:
    path = Path(path)
    data = path.read_bytes()            # read ONCE: hash, parse and written cells all use these bytes
    sha = sha256_of(data)
    if apply and expect_sha256 != sha:
        out("REFUSED: --apply needs --expect-sha256 equal to this file's sha256 "
            "(run the dry run first). Nothing was written.")
        return 2
    conn = _connect(dsn)
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
    finally:
        conn.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--year", type=int, required=True, help="year of the FIRST date column")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--expect-sha256")
    a = ap.parse_args(argv)
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        sys.exit("SUPABASE_DB_URL is not set (environment only).")
    sys.exit(run(dsn, a.run_id, a.file, a.year, a.apply, a.expect_sha256))


if __name__ == "__main__":
    main()
