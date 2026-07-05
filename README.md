# Attendance Absence Verification System

Parses the monthly attendance workbook, creates verification **cases** for disputable
attendance days, lets **Team Leaders** confirm/correct them via a persistent per-TL web page,
and lets **HRBP** close or override before the results merge back into payroll.

See [`SPEC.md`](./SPEC.md) for the full design contract.

## Status

Build Order (SPEC §9):

- [x] **1. Schema + RLS + status vocabulary seed** ← current
- [ ] 2. Reference seed from `HC` + `Structure`
- [ ] 3. Ingestion function + `Summary Report` parser
- [ ] 4. TL verification page
- [ ] 5. Email dispatch + notification tracking
- [ ] 6. HRBP dashboard
- [ ] 7. Manual period-close + audit/exceptions views

## Layout

```
SPEC.md                         design contract (v2)
supabase/migrations/            Postgres schema, RLS, seeds
tools/gen_status_vocabulary.py  regenerates the vocabulary seed from a real workbook
data/                           local attendance workbooks (git-ignored — never commit PII)
```

## Regenerating the status vocabulary

When a new period's workbook introduces new status strings, refresh the seed:

```bash
python tools/gen_status_vocabulary.py \
  --src "data/Attendance_Report.xlsx" \
  --out supabase/migrations/<timestamp>_seed_status_vocabulary.sql
```

The script prints an `UNCLASSIFIED` list — any value it can't confidently bucket lands in
`ignore` and should be reviewed (mapped in the `status_vocabulary` table via the dashboard).

## Applying migrations

With the [Supabase CLI](https://supabase.com/docs/guides/cli):

```bash
supabase db reset      # local: apply all migrations + seeds
supabase db push       # remote: apply pending migrations
```
