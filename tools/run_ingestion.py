"""One-command monthly ingestion: parse the workbook and load managers, employees, cases, and
exceptions into Supabase.

  SUPABASE_DB_URL=postgresql://... python tools/run_ingestion.py --src "data/report.xlsx" --year 2026

SUPABASE_DB_URL is the project's Postgres connection string (Dashboard → Settings → Database).
It is a secret — keep it in .env (git-ignored), never commit it."""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load_env_file(path=".env"):
    """Minimal .env reader (no shell interpretation, so passwords with special chars are safe)."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


from ingestion import loader                          # noqa: E402
from ingestion.config import load_aliases             # noqa: E402
from ingestion.db_psycopg import PsycopgDB, connect   # noqa: E402
from ingestion.reference import parse_reference       # noqa: E402
from ingestion.summary import ingest_summary          # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="attendance workbook (.xlsx)")
    ap.add_argument("--year", type=int, required=True, help="year for the Summary Report date columns")
    ap.add_argument("--dry-run", action="store_true", help="parse and report, but do not write to the DB")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    _load_env_file()

    ref = parse_reference(args.src, aliases=load_aliases())
    res = ingest_summary(args.src, ref, year=args.year)
    print(f"Parsed: {ref.stats}")
    print(f"        cases={res.stats['cases']} exceptions={res.stats['exceptions']} "
          f"days_by_bucket={res.stats['days_by_bucket']}")

    if args.dry_run:
        print("\n[dry-run] nothing written.")
        return

    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        sys.exit("SUPABASE_DB_URL is not set — export the Postgres connection string first.")

    conn = connect(dsn)
    try:
        with conn:  # commits on success, rolls back on exception
            db = PsycopgDB(conn)
            loader.load_reference(db, ref)
            summary = loader.load_ingestion(
                db, res, reference=ref, source_filename=os.path.basename(args.src))
        print(f"\nLoaded: run={summary.run_id} managers={len(ref.managers)} "
              f"employees={len(ref.employees)} cases={summary.cases} exceptions={summary.exceptions}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
