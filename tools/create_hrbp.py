"""Create (or reset) an HRBP login. Run once to bootstrap the first admin.

  SUPABASE_DB_URL=postgresql://... python tools/create_hrbp.py \
      --email you@51talk.com --name "Your Name" --password "…"
"""
import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import data                       # noqa: E402
from ingestion.db_psycopg import connect   # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--password", help="omit to be prompted securely")
    args = ap.parse_args()

    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        sys.exit("SUPABASE_DB_URL is not set.")
    password = args.password or getpass.getpass("Password: ")
    conn = connect(dsn)
    data.create_hrbp(conn, args.email, args.name, password)
    conn.close()
    print(f"HRBP ready: {args.email}")


if __name__ == "__main__":
    main()
