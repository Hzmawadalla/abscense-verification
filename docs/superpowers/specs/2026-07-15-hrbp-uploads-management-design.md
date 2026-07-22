# Design: HRBP front-end upload management (remove upload + start-fresh reset)

Date: 2026-07-15
Status: Approved

## Problem

When an HRBP ingests a wrong attendance sheet, the mistaken cases accumulate (the loader upserts by
`(employee, date)` and never deletes days a later sheet drops), and there is no front-end way to undo
it. Today the only remedies are backend DB surgery (delete the run's cases by `created_at`) or a full
manual wipe. HRBP needs to do both from the UI: **remove a single upload's data** and **clear
everything to start fresh** — safely, without touching reference data (managers, employees, TL
tokens, vocabulary, login).

## Approach

Tag every case with the upload that created it (`cases.ingestion_run_id`), then expose an **Uploads**
tab that lists uploads with case counts and offers a guarded per-upload **Remove** plus a guarded
**Clear everything** reset. All deletions run in app code behind confirmations — the schema FK is
`ON DELETE SET NULL` so a run deletion can never silently cascade away cases.

## Components

### 1. Schema — migration `..._cases_ingestion_run_id.sql`

```sql
set search_path = attendance, public;

alter table attendance.cases
  add column if not exists ingestion_run_id uuid
    references attendance.ingestion_runs(id) on delete set null;

create index if not exists cases_ingestion_run_id_idx on attendance.cases(ingestion_run_id);
```

**Backfill (one-time, in the migration):** assign each existing case to the run whose `created_at`
is the latest at or before the case's `created_at` — the run active when the case was inserted.
Exact for the current data (166 cases, single 07-12 run); best-effort for any legacy multi-run data.

```sql
update attendance.cases c
set ingestion_run_id = (
  select r.id from attendance.ingestion_runs r
  where r.created_at <= c.created_at
  order by r.created_at desc
  limit 1)
where c.ingestion_run_id is null;
```

### 2. Loader — `ingestion/loader.py`

- `UPSERT_CASE` gains `ingestion_run_id` in the insert column list and values.
- On conflict `(employee_id, work_date)` the update clause **does not** touch `ingestion_run_id`
  (creator-owns: a later upload re-touching a day keeps the original owner).
- `load_summary` (or the case-loading path) is passed the current `ingestion_run_id` and includes it
  in each case's params.

### 3. Data layer — `app/data.py`

- **`list_uploads(conn) -> list[dict]`** — one row per `ingestion_runs`, newest first:
  `{id, source_filename, created_at, total, verified, open}` where `verified` counts
  `manager_status is not null` and `open` counts `status = 'open'`, joined via
  `cases.ingestion_run_id`.
- **`remove_upload(conn, run_id) -> dict`** — `delete from cases where ingestion_run_id = %s`, then
  `delete from ingestion_runs where id = %s` (its `ingestion_exceptions` cascade). Returns
  `{cases_deleted, verified_deleted}`. Commits.
- **`reset_all_cases(conn) -> dict`** — delete all rows from `cases`, `ingestion_exceptions`,
  `ingestion_runs` (managers, employees, status_vocabulary, hrbp_users untouched). Returns counts.
  Commits.

### 4. UI — `streamlit_app.py`

A new **🗂️ Uploads** tab in the HRBP view:

- A table from `list_uploads`: file · date · total · verified · open.
- Per upload, a **Remove** control. When the upload has `verified > 0`, it shows
  *"⚠️ This permanently deletes N verified verdict(s)"* and requires **both** ticking an
  acknowledgement checkbox **and** typing `REMOVE` before the button enables. With no verified cases,
  a single confirm checkbox suffices.
- A separate **"Clear everything & start fresh"** section that deletes all cases/exceptions/uploads,
  gated by typing `CLEAR` (mirrors the period-close `CLOSE` pattern). It states plainly that
  reference data (managers, employees, links, vocabulary, login) is preserved.

### 5. Not in scope (YAGNI)

Per-case deletion, undo of a removal, editing an upload, and any change to links (tokens belong to
managers, not uploads) are out of scope.

## Testing

- **Loader tags cases:** `UPSERT_CASE` includes `ingestion_run_id`; a fake-DB test asserts each case
  param carries the run id, and that the conflict clause does not update it.
- **`remove_upload`:** deletes only the target run's cases, leaving another upload's cases (including
  verified ones) intact; returns the correct `verified_deleted` count. Fake-connection test.
- **`reset_all_cases`:** clears cases/exceptions/runs; a managers row is untouched. Fake-connection
  test.
- **`list_uploads`:** returns per-run total/verified/open counts. Fake-connection test.
- Existing suite stays green. The Streamlit tab is verified by manual smoke (list shows uploads;
  removing one with verified cases requires the tick + typed `REMOVE`; reset requires `CLEAR`).

## Deployment (later — not part of this task)

1. Commit the migration; apply it to the live Supabase DB (additive column + index + backfill).
2. Commit code; push to `master` → Community Cloud redeploys.

No new secrets. No dependency changes.
