"""Smoke-run the reference parser against a real workbook and print a summary.
Not a test (depends on a local PII file); a sanity check for real-world behavior.

  python tools/probe_reference.py --src "data/Attendance_Report.xlsx"
"""
import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ingestion.config import load_aliases  # noqa: E402
from ingestion.reference import parse_reference  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    ref = parse_reference(args.src, aliases=load_aliases())
    print("STATS:", ref.stats)

    reasons = collections.Counter(e.reason for e in ref.exceptions)
    print("\nEXCEPTIONS by reason:")
    for r, c in reasons.most_common():
        print(f"   {c:5d}  {r}")

    mapped = sum(1 for e in ref.employees if e.manager_crm)
    print(f"\nEmployees with a TL: {mapped}/{len(ref.employees)}")
    no_email = sum(1 for m in ref.managers if not m.email)
    print(f"Managers (TLs): {len(ref.managers)}   without email: {no_email}")

    print("\nSample managers:")
    for m in ref.managers[:5]:
        print(f"   {m.crm!r:24} name={m.name!r:22} email={m.email!r}")
    print("\nSample employees:")
    for e in ref.employees[:5]:
        print(f"   {e.crm!r:20} TL={e.manager_crm!r:20} dept={e.department!r:12} status={e.employee_status!r}")


if __name__ == "__main__":
    main()
