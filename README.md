# Attendance Absence Verification System

Parses the monthly attendance workbook, creates verification **cases** for disputable
attendance days, lets **Team Leaders** confirm/correct them via a persistent per-TL web page,
and lets **HRBP** close or override before the results merge back into payroll.

See [`SPEC.md`](./SPEC.md) for the full design contract.

## Status

Build Order (SPEC §9):

- [x] 1. Schema + RLS + status vocabulary seed  → **deployed** (Supabase `attendance` schema)
- [x] 2. Reference parser (`HC` + `Structure`)
- [x] 3. Ingestion parser (`Summary Report` → cases)
- [x] 4. DB loader + in-app ingestion (upload workbook → cases)
- [x] 6. TL verification page + HRBP dashboard + exceptions + period-close (Streamlit)
- [ ] 5. Email dispatch of TL links + notification tracking (links are generated in-app; sending TBD)
- [ ] 7. Attachment uploads to Supabase Storage (TL comment works; file upload TBD)

The app is **Streamlit** (Python), reusing the `ingestion/` package. Two access layers:
HRBP (email+password) and TL (unique `?t=<token>` link).

## Running the app

```bash
pip install -r requirements.txt
# one-time: create the first HRBP login
SUPABASE_DB_URL=postgresql://... python tools/create_hrbp.py --email you@x.com --name "You"
# run
SUPABASE_DB_URL=postgresql://... streamlit run streamlit_app.py
```

Configure secrets in `.streamlit/secrets.toml` (see `.streamlit/secrets.toml.example`) or, on
Streamlit Community Cloud, in the app's Secrets manager.

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
