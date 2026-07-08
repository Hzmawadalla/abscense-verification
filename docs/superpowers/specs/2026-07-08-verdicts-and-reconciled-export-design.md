# Design: 6-option verdicts + reconciled attendance export

Date: 2026-07-08
Status: Approved

## Problem

Two related needs on the HRBP/TL attendance-verification flow:

1. TLs (and HRBP overrides) need a richer, explicit verdict set instead of the current
   three coded values plus a free-text leave field.
2. After processing, HRBP needs to export a **reconciled attendance report**: the original
   attendance matrix with verified cases overwritten by their final verdict, plus a change
   log of what moved — for payroll.

A third need (persisting TL verdicts across re-uploads and reconciling them) is **already
satisfied** by the existing idempotent loader and is documented here for completeness.

## Part A — 6-option verdict

Replace the current TL/HRBP verdict set with six explicit options.

| UI label      | stored enum code |
|---------------|------------------|
| Present       | `present`        |
| Annual Leave  | `annual_leave`   |
| Unpaid Leave  | `unpaid_leave`   |
| Sick Leave    | `sick_leave`     |
| Absent        | `absent`         |
| Half Day      | `half_day`       |

- **Migration:** extend the `verdict` enum with `annual_leave, unpaid_leave, sick_leave,
  half_day`. `present`/`absent` retained; legacy `leave` retained but unused (Postgres cannot
  cleanly drop an enum value; it is harmless). No data migration — verdict columns are empty.
- **TL page:** swap the 3-option dropdown for the 6; **remove** the conditional free-text
  "Leave type" box (Annual/Unpaid/Sick are explicit now). Comment + attachment upload unchanged.
- **HRBP side:** the "Override to" dropdown reads the shared verdict map, so it becomes the same
  6 automatically. Period-close still auto-closes unanswered cases as `absent`.
- `half_day` and `sick_leave` flow into `final_status` and the export as-is; half-day pay math
  is a downstream/manual step.

## Part B — Reconciled attendance report export

A new **Export** tab in the HRBP view.

1. HRBP re-uploads the month's attendance workbook (same Summary Report). The server does not
   retain the originally-uploaded file, so re-upload is required (auto-save-at-ingest is a
   deferred enhancement).
2. "Build reconciled report" produces `Attendance_Reconciled_<period>.xlsx` with two sheets:

**Sheet 1 — "Updated Attendance":** the original matrix, with each **closed** case's cell
overwritten by its final verdict label (e.g. `Ahmed Ali · 26-Jun: Absent → Annual Leave`).
Matched by CRM (case-insensitive, same normalization as ingestion) × date column. All other
cells untouched.

**Sheet 2 — "Changes":** one row per **closed** case — `CRM · Employee · Date · Before
(source status) · After (final verdict) · Verified by · Comment`.

**Scope:** closed cases only (`final_status`).

### Components

- `app/report.py` — `build_reconciled_report(matrix_path, closed_cases, year) -> bytes`.
  Isolated, unit-testable. Reuses header-row detection and CRM normalization helpers.
- `app/data.py` — `list_closed_cases(conn)` returning CRM, work_date, source_status,
  final_status, closed_by, manager_comment, employee_name, manager_name.
- `streamlit_app.py` — `VERDICTS` (6 entries) + `VERDICT_LABEL` reverse map; new Export tab;
  removal of the leave-type sub-field.

## Part C — Reconciliation on re-ingest (already implemented)

`cases` has `UNIQUE (employee_id, work_date)`. The loader upserts with
`ON CONFLICT (employee_id, work_date) DO UPDATE SET source_status, is_half_day`, leaving
`status, manager_status, manager_comment, final_status, manager_responded_at` untouched. Thus a
re-upload re-matches by (employee, date), inserts new flagged days, refreshes source status, and
preserves any TL verdict already submitted. No new code required.

**Known nuance (deferred):** a day flagged in an earlier run but no longer flagged in a later
re-upload remains open — the upsert does not auto-close dropped-out days.

## Testing

- Unit test for `build_reconciled_report`: given a small matrix workbook + a set of closed
  cases, assert the correct cells are overwritten (Sheet 1) and the change log rows are correct
  (Sheet 2), including CRM case-insensitive matching and date-column matching.
- Existing suite (ingestion/reference/summary/security/storage/dingtalk) must stay green; the
  verdict change is UI-level and does not touch those.

## Deployment

1. Add + commit the enum migration; apply it to the live Supabase DB (ALTER TYPE ... ADD VALUE,
   autocommit — cannot be used in the same transaction that later reads the new values).
2. Commit code changes; push to `master` → Community Cloud redeploys.
