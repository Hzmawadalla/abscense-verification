"""Emit SQL to load a workbook's parsed data into the attendance schema (for one-off MCP loads).

Produces set-based, schema-qualified, idempotent statements into a target dir:
  managers.sql · employees_N.sql (batched) · cases.sql · exceptions.sql
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ingestion.config import load_aliases          # noqa: E402
from ingestion.reference import parse_reference     # noqa: E402
from ingestion.summary import ingest_summary        # noqa: E402


def lit(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    return "'" + str(v).replace("'", "''") + "'"


def row(vals):
    return "(" + ", ".join(lit(v) for v in vals) + ")"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    ref = parse_reference(args.src, aliases=load_aliases())
    res = ingest_summary(args.src, ref, year=args.year)
    head = "set search_path = attendance, public;\n\n"

    # managers
    mrows = ",\n".join(row((m.crm, m.name, m.email)) for m in ref.managers)
    (out / "managers.sql").write_text(
        head + "insert into managers (crm, name, email, active) values\n" + mrows +
        "\non conflict (crm) do update set name=excluded.name, "
        "email=coalesce(excluded.email, managers.email), active=true;\n", encoding="utf-8")

    # employees (batched; manager_id resolved by subselect)
    ecols = ("crm, ps_id, name, email, department, vendor, team, manager_crm, sm_crm, "
             "employee_status, join_date, exit_date")
    emps = ref.employees
    batch = 200
    for i in range(0, len(emps), batch):
        chunk = emps[i:i + batch]
        vals = ",\n".join(row((e.crm, e.ps_id, e.name, e.email, e.department, e.vendor, e.team,
                                e.manager_crm, e.sm_crm, e.employee_status,
                                str(e.join_date) if e.join_date else None,
                                str(e.exit_date) if e.exit_date else None)) for e in chunk)
        sql = (head +
               "insert into employees (crm, ps_id, name, email, department, vendor, team, "
               "manager_id, sm_crm, employee_status, join_date, exit_date)\n"
               "select v.crm, v.ps_id, v.name, v.email, v.department, v.vendor, v.team, "
               "(select m.id from managers m where lower(m.crm)=lower(v.manager_crm)), "
               "v.sm_crm, v.employee_status, v.join_date::date, v.exit_date::date\n"
               f"from (values\n{vals}\n) as v({ecols})\n"
               "on conflict (crm) do update set ps_id=excluded.ps_id, name=excluded.name, "
               "email=excluded.email, department=excluded.department, vendor=excluded.vendor, "
               "team=excluded.team, manager_id=excluded.manager_id, sm_crm=excluded.sm_crm, "
               "employee_status=excluded.employee_status, join_date=excluded.join_date, "
               "exit_date=excluded.exit_date;\n")
        (out / f"employees_{i // batch + 1}.sql").write_text(sql, encoding="utf-8")

    # cases
    crows = ",\n".join(row((c.employee_crm, c.manager_crm, str(c.work_date), c.source_status, c.is_half_day))
                       for c in res.cases)
    (out / "cases.sql").write_text(
        head + "insert into cases (employee_id, manager_id, work_date, source_status, is_half_day)\n"
        "select (select e.id from employees e where lower(e.crm)=lower(v.ecrm)), "
        "(select m.id from managers m where lower(m.crm)=lower(v.mcrm)), "
        "v.work_date::date, v.source_status, v.is_half_day\n"
        f"from (values\n{crows}\n) as v(ecrm, mcrm, work_date, source_status, is_half_day)\n"
        "on conflict (employee_id, work_date) do update set source_status=excluded.source_status, "
        "is_half_day=excluded.is_half_day;\n", encoding="utf-8")

    # exceptions (ingestion + reference), tied to one run
    exc = [(e.crm, str(e.work_date) if e.work_date else None, e.raw_value, e.reason) for e in res.exceptions]
    exc += [(e.crm, None, e.detail, e.reason) for e in ref.exceptions]
    xrows = ",\n".join(row(t) for t in exc)
    (out / "exceptions.sql").write_text(
        head +
        "with run as (insert into ingestion_runs (source_filename, created_count, skipped_count, "
        f"exception_count) values ('{Path(args.src).name}', {len(res.cases)}, 0, {len(exc)}) returning id)\n"
        "insert into ingestion_exceptions (ingestion_run_id, crm, work_date, raw_value, reason)\n"
        "select run.id, v.crm, v.work_date::date, v.raw_value, v.reason\n"
        f"from run, (values\n{xrows}\n) as v(crm, work_date, raw_value, reason);\n", encoding="utf-8")

    n_emp_files = (len(emps) + batch - 1) // batch
    print(f"managers={len(ref.managers)} employees={len(emps)} (in {n_emp_files} file(s)) "
          f"cases={len(res.cases)} exceptions={len(exc)}")
    for f in sorted(out.glob("*.sql")):
        print(f"  {f.name}: {f.stat().st_size} bytes")


if __name__ == "__main__":
    main()
