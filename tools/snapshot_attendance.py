"""Pre-migration SAFETY SNAPSHOT of the `attendance` schema — read-only. NOT a database backup.

Copies every table in the `attendance` schema to CSV (credential columns excluded), plus a listing (names and sizes, not the
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
# Credentials are never copied: HRBP password hashes and TL link tokens (hash + encrypted).
SECRET_COLUMNS = {"password_hash", "access_token_hash", "access_token_enc"}


def _tables(cur) -> list[str]:
    cur.execute("select table_name from information_schema.tables "
                "where table_schema = 'attendance' and table_type = 'BASE TABLE' "
                "order by table_name")
    return [r[0] for r in cur.fetchall()]


def _columns(cur, table: str) -> list[str]:
    cur.execute("select column_name from information_schema.columns "
                "where table_schema = 'attendance' and table_name = %s "
                "order by ordinal_position", (table,))
    return [r[0] for r in cur.fetchall() if r[0] not in SECRET_COLUMNS]


def _open_private(path: Path):
    """Create a new file readable by its owner only; never overwrite an existing one."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    return os.fdopen(fd, "wb")


def _copy_table(cur, table: str, path: Path) -> None:
    # names come from information_schema, but quote them anyway
    cols = ", ".join(f'"{c}"' for c in _columns(cur, table))
    with _open_private(path) as f, cur.copy(
            f'copy (select {cols} from attendance."{table}") to stdout with csv header') as cp:
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
    out.mkdir(parents=True, mode=0o700)
    os.chmod(out, 0o700)   # mkdir's mode is masked by umask

    conn = connect(dsn)
    conn.read_only = True
    try:
        with conn.cursor() as cur:
            counts = {}
            for t in _tables(cur):
                _copy_table(cur, t, out / f"{t}.csv")
                cur.execute(f'select count(*) from attendance."{t}"')
                counts[t] = cur.fetchone()[0]
            with _open_private(out / "storage_objects.csv") as f, cur.copy(
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
