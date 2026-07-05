"""Smoke-run the full ingestion (reference + summary) against a real workbook.

  python tools/probe_summary.py --src "data/Attendance_Report.xlsx" --year 2026
"""
import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ingestion.config import load_aliases          # noqa: E402
from ingestion.reference import parse_reference     # noqa: E402
from ingestion.summary import ingest_summary        # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--year", type=int, required=True)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    ref = parse_reference(args.src, aliases=load_aliases())
    res = ingest_summary(args.src, ref, year=args.year)

    print("Days by bucket:", res.stats["days_by_bucket"])
    print(f"\nCases created: {res.stats['cases']}")
    print(f"Exceptions:    {res.stats['exceptions']}")

    reasons = collections.Counter(e.reason for e in res.exceptions)
    print("\nException reasons:")
    for r, c in reasons.most_common():
        print(f"   {c:5d}  {r}")

    by_status = collections.Counter(c.source_status for c in res.cases)
    print("\nCases by source_status:")
    for s, c in by_status.most_common():
        print(f"   {c:5d}  {s}")

    print("\nSample cases:")
    for c in res.cases[:6]:
        print(f"   {c.employee_crm!r:20} {c.work_date}  TL={c.manager_crm!r:22} {c.source_status!r}")

    unknown = [e for e in res.exceptions if e.reason == "unknown_status"]
    if unknown:
        seen = collections.Counter(e.raw_value for e in unknown)
        print("\nUnknown statuses (would need mapping):")
        for v, c in seen.most_common(15):
            print(f"   {c:5d}  {v!r}")


if __name__ == "__main__":
    main()
