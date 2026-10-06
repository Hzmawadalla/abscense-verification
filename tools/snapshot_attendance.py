"""Pre-migration SAFETY SNAPSHOT of the `attendance` schema — read-only. NOT a database backup.

Copies every table in the `attendance` schema to CSV, plus a listing (names and sizes, not the
files) of the evidence bucket, into a timestamped folder OUTSIDE the repo. It exists so a
reviewer can compare row counts and spot-check values before and after a migration.

It is not a recovery mechanism: it captures no schema, types, constraints, sequences or storage
objects, and restoring from it would be a manual, lossy exercise. Before any production migration
the real recovery path for the Supabase project (plan backups / PITR / a pg_dump taken with the
matching server version) must be identified separately.

The output contains employee data — keep it out of git and delete it once the migration is
verified.

  SUPABASE_DB_URL=postgresql://... python tools/snapshot_attendance.py [--out DIR]
"""
import argparse
import datetime
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from ingestion.db_psycopg import connect   # noqa: E402

DEFAULT_OUT = Path.home() / "attendance-snapshots"
BUCKET = "case-attachments"


def _tables(cur) -> list[str]:
    cur.execute("select table_name from information_schema.tables "
                "where table_schema = 'attendance' and table_type = 'BASE TABLE' "
                "order by table_name")
    return [r[0] for r in cur.fetchall()]


def _copy_table(cur, table: str, path: Path) -> None:
    # table comes from information_schema, but quote it anyway
    with path.open("wb") as f, cur.copy(
            f'copy (select * from attendance."{table}") to stdout with csv header') as cp:
        for chunk in cp:
            f.write(chunk)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        sys.exit("SUPABASE_DB_URL is not set.")
    out = args.out / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    if REPO_ROOT in out.resolve().parents:
        sys.exit(f"Refusing to write employee data inside the repo: {out}")
    out.mkdir(parents=True)

    conn = connect(dsn)
    conn.read_only = True
    try:
        with conn.cursor() as cur:
            counts = {}
            for t in _tables(cur):
                _copy_table(cur, t, out / f"{t}.csv")
                cur.execute(f'select count(*) from attendance."{t}"')
                counts[t] = cur.fetchone()[0]
            with (out / "storage_objects.csv").open("wb") as f, cur.copy(
                    "copy (select name, (metadata->>'size')::bigint as size_bytes, created_at "
                    "from storage.objects where bucket_id = %s order by name) "
                    "to stdout with csv header", (BUCKET,)) as cp:
                for chunk in cp:
                    f.write(chunk)
    finally:
        conn.close()

    for t, n in counts.items():
        print(f"{t:28} {n:>8} rows")
    print(f"Snapshot (not a backup) written to {out}")


if __name__ == "__main__":
    main()
