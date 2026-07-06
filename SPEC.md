# Attendance Absence Verification System — Spec v2

> Supersedes the original architecture draft. This version is reconciled against the real
> source file `Attendance_Report_May-Jun V.3.xlsx` and the decisions taken during design review.
> No code yet — this is the approved design contract for Build Order step 1.

## 1. Purpose

Each pay period, parse the attendance workbook, identify **disputable attendance days**
(unexplained absences and unconfirmed leaves), and create one verification **case** per
employee-day. Each **Team Leader (TL)** confirms/corrects their team's cases via a persistent,
per-TL web page. **HRBP** reviews replies and closes or overrides. Verified verdicts merge back
into the monthly attendance report. Unresolved cases stand as **Absent** when HRBP closes the period.

The system replaces today's manual process: the per-manager `*Verification` tabs, the freeform
`Notes` tab, and the in-cell "To Be Confirmed" annotations people currently hand-type.

## 2. Stack

- **Streamlit** (Python): single app hosting both layers, on **Streamlit Community Cloud**.
  All ingestion/parsing/loader logic is the same Python package (`ingestion/`), reused directly.
  - **HRBP layer** — per-user email+password login (backed by `hrbp_users`, bcrypt hashes;
    `streamlit-authenticator`). Access: ingest, dashboard, close/override, exceptions.
  - **TL layer** — unique link `…/?t=<token>`, no login; the token is the credential. Sees only
    their own team's open cases.
- **Supabase**: Postgres (data) + Storage (TL attachments), reached from Streamlit via a direct
  Postgres connection (`SUPABASE_DB_URL` in Streamlit secrets). Tables live in the `attendance`
  schema; RLS denies anon/authenticated, so only this server-side connection reads/writes them.
- **DingTalk**: TL links are delivered as private work-notification DMs via a DingTalk corporate
  internal app (identified by each TL's DingTalk userid). Every send is recorded in `notifications`
  (channel `dingtalk`). Config: `DINGTALK_APP_KEY` / `_SECRET` / `_AGENT_ID` in secrets;
  CRM→userid mapping in `config/tl_dingtalk.json`.
- **Attendance source**: workbook uploaded **in-app** by HRBP each period (no CLI). Ingestion sits
  behind an interface so an **iTalent** source can replace the Excel later without touching
  case/verification logic.
- No scheduler — ingestion and period-close are both HRBP-triggered in-app.

## 3. Data Sources (all tabs keyed on `CRM`)

| Tab | Seeds / Feeds | Notes |
|---|---|---|
| `HC` (1,325 rows) | `employees` master | CRM, Full Name, **Work Email**, Department, Vendor, PS ID, `Employee Status`, Join/Exit Date. Drop `Departed`. |
| `Structure` (337 rows) | manager mapping | `SM/LTL/STL → TL/Coach Lead → Bigteam → Team → CRM`. **TL = the verifier.** SM stored for reference only. |
| `Summary Report` | candidate cases | Wide matrix: `CRM · Normal Days · Abnormal Days · 06-May · 07-May · …`. Each day-cell is a status string. Unpivot wide→long. |
| `Leave Balance` | **ignored** | Not used at ingestion (decision). |
| `*Verification`, `Notes` | reference only | The manual process being replaced; model for verdict taxonomy + evidence types. |

Decision: a clean coded leave (`Annual Leave`, `Sick Leave`, etc.) already means **approved** —
no balance check, no verification.

## 4. Case-Generation Rules (deterministic status → bucket)

The parser normalizes each day-cell (trim, collapse case, strip `(HD)`/half-day markers into a flag)
and looks it up in the `status_vocabulary` table. Every raw value maps to exactly one bucket.

### SKIP — no case (already clean/resolved)
`Normal`, `Weekend`, `Public Holiday`, `Not Yet Hired`, `Present`, `No Leave`, `Leave Approved`,
and **all clean coded leaves**: `Annual Leave`, `Sick Leave`, `Casual Leave`,
`Continuing Education Leave`, `Paternity Leave`, `Bereavement Leave`, `Unpaid Leave`
(and their `(HD)` variants). A clean `Unpaid Leave` is still an *approved* code (just unpaid),
so it skips; only `Unpaid Leave (Failed)` triggers.

### NOT VERIFIED THIS STAGE — deduct as-coded, no case
`Late`, `Missing Punch Out`, `Half Day` (incl. `Halfday`/`Hlaf day`), `2 Hour Excuse`, `Excuse`.
(They still drive payroll deductions; they just don't reach a TL at this stage.)

### TRIGGER — create a verification case
`Absent`, `No Show`, **all `(Failed)` variants**, and **all mid-dispute annotations**:
`… - To Be Confirmed`, `… - Check`, `Leave Approval Pending`, `… To Be deducted from Balance`,
`Applied on leave on <date>`, `Sick Leave (Returned)`.

> Rule of thumb (this stage): **verify only a genuine unexplained absence (`Absent`/`No Show`)
> or a leave that is explicitly unconfirmed/failed.** Everything already coded as an approved
> state — approved leaves (incl. clean `Unpaid Leave`) and minor partial-day deductions
> (`Late`, `Half Day`, `2 Hour Excuse`) — is trusted as-is. The annotation suffix (`(Failed)`,
> `To Be Confirmed`, `Check`) is the "not resolved" signal, independent of the base word.

### EXCEPTIONS — route to review queue, NEVER a case
`Departed` (also excluded via `HC.Employee Status`), `#N/A`, `0`, bare `1/2/3`, stray names
(`Felix`, `Zimmy`, `Jimmy`, `Helen`), team codes (`ME-EGSS0x大组`), CRM-like values
(`EGSS-hussienmo`), `Resigned`, **and any value not present in `status_vocabulary`.**

New/unknown strings surface in `ingestion_exceptions` for HRBP to map — they are never silently
turned into deductions.

## 5. Data Model (Postgres)

### `hrbp_users` — authorization allowlist
Authentication (Supabase Auth) ≠ authorization. Only emails here may use the dashboard.

| col | type | notes |
|---|---|---|
| id | uuid PK | |
| email | text unique | matches Supabase Auth email |
| active | bool | |

### `managers` — the Team Leaders (verifiers)
| col | type | notes |
|---|---|---|
| id | uuid PK | |
| crm | text unique | TL's CRM from `Structure` |
| name | text | |
| email | text | from `HC` |
| access_token_hash | text unique | `sha256(token)`; raw token exists only in the emailed link |
| active | bool | supports rotation (regenerate → resend) |

### `employees`
| col | type | notes |
|---|---|---|
| id | uuid PK | |
| crm | text unique | primary business key |
| ps_id | text | payroll id |
| name | text | |
| email | text | from `HC` |
| department | text | |
| vendor | text | JHR / Migrate / SKY / … |
| team | text | from `Structure` |
| manager_id | uuid FK → managers | current TL |
| sm_crm | text nullable | senior manager, reference only |
| employee_status | text | Active / Departed |
| join_date | date nullable | |
| exit_date | date nullable | |

### `cases`
| col | type | notes |
|---|---|---|
| id | uuid PK | |
| employee_id | uuid FK | |
| manager_id | uuid FK | denormalized TL at creation time (survives later reassignment) |
| work_date | date | the disputed day (local business date) |
| source_status | text | normalized raw status, e.g. `Absent`, `Unpaid Leave`, `Annual Leave (Failed)` |
| is_half_day | bool | parsed from `(HD)` marker |
| status | enum | `open`, `manager_responded`, `closed` |
| manager_status | enum nullable | `present`, `absent`, `leave` |
| leave_type | text nullable | when `leave`: annual/sick/unpaid/… |
| manager_comment | text nullable | |
| manager_responded_at | timestamptz nullable | |
| final_status | enum nullable | HRBP decision: `present`, `absent`, `leave` |
| final_leave_type | text nullable | |
| closed_by | text nullable | `hrbp` \| `hrbp_cutoff` |
| closed_at | timestamptz nullable | |
| created_at | timestamptz | |
| updated_at | timestamptz | drives optimistic locking |
| | | **UNIQUE(employee_id, work_date)** — idempotent re-runs |

### `case_attachments`
One case can have several attachments (medical cert + email screenshot, etc.).

| col | type | notes |
|---|---|---|
| id | uuid PK | |
| case_id | uuid FK | |
| storage_path | text | private Supabase Storage path |
| filename | text | |
| content_type | text | validated allowlist: pdf/jpg/png |
| size_bytes | int | capped (10 MB) |
| uploaded_at | timestamptz | |

### `status_vocabulary` — data-driven parser rules
Lets HRBP map new raw strings without a code change.

| col | type | notes |
|---|---|---|
| id | uuid PK | |
| raw_value | text unique | normalized key |
| bucket | enum | `skip`, `not_verified`, `trigger`, `exception` |
| canonical_status | text nullable | e.g. `Absent`, `Unpaid Leave` |
| active | bool | |

### `ingestion_runs`
| col | type | notes |
|---|---|---|
| id | uuid PK | |
| source_filename | text | |
| range_start / range_end | date | |
| triggered_by | text | HRBP email |
| created_count / skipped_count / exception_count | int | summary |
| created_at | timestamptz | |

### `ingestion_exceptions`
| col | type | notes |
|---|---|---|
| id | uuid PK | |
| ingestion_run_id | uuid FK | |
| crm | text | raw, may be unmapped |
| work_date | date nullable | |
| raw_value | text | the unrecognized cell / unmapped employee |
| reason | text | `unknown_status` \| `unmapped_employee` \| … |
| resolved | bool | |
| resolved_by | text nullable | |
| created_at | timestamptz | |

### `notifications` — the deliverability safety net
Email is a *nudge*, not the system of record (the TL link is persistent). But silent email
failure must never cause a wrongful auto-Absent, so every send is tracked.

| col | type | notes |
|---|---|---|
| id | uuid PK | |
| manager_id | uuid FK | |
| ingestion_run_id | uuid FK nullable | |
| channel | text | `email` |
| case_count | int | cases in this nudge |
| provider_message_id | text nullable | Resend id |
| status | enum | `queued`, `sent`, `delivered`, `bounced`, `failed` |
| error | text nullable | |
| sent_at | timestamptz | |

Dashboard surfaces **"TLs with open cases not successfully notified"** so a human catches failures
before cutoff. The `max 1 email/TL/day` cap is enforced from this table, not in-memory.

### `audit_log` — append-only, disputable-record grade
`id, case_id, actor (TL token id \| hrbp email \| 'system'), action, old_value jsonb,
new_value jsonb, created_at`. JSONB field-level diffs; indexed on `case_id`.

## 6. Flows

### 6.1 Seed / refresh reference data (HRBP-triggered)
1. HRBP uploads the workbook.
2. `HC` → upsert `employees` (skip `Departed`).
3. `Structure` → upsert `managers` (TLs) and set each `employees.manager_id`.
   New TLs get an `access_token` (store hash, email link on first activation).
4. Unmapped employees (in Summary but not HC, or no TL in Structure) → `ingestion_exceptions`.

### 6.2 Ingestion (HRBP-triggered, per pay period — not a daily cron)
1. HRBP uploads workbook + selects date range.
2. Parser unpivots `Summary Report` wide→long; for each employee-day, normalize the status and
   look up `status_vocabulary`:
   - `skip` / `not_verified` → no case.
   - `trigger` → upsert `cases` (UNIQUE(employee_id, work_date) makes re-runs idempotent).
   - `exception` / unknown → `ingestion_exceptions`.
3. Skip any `work_date` in an already-closed month (warn in summary).
4. Write `ingestion_runs` summary: created / skipped / exceptions.
5. For each TL with ≥1 open case: send **one** reminder email → `https://site/verify?t={token}`,
   logged in `notifications` (cap 1/TL/day).

### 6.3 TL verification page (bearer token)
1. Page loads → Function validates `sha256(token)` → returns that TL's `open` cases.
2. Table per case: employee, date, `source_status`, verdict dropdown (**Present / On Leave / Absent**),
   leave-type (if Leave), comment, multi-file attachment. Single submit for edited rows.
3. Function writes verdict, sets `status = manager_responded`, logs audit.
   **Optimistic lock:** `UPDATE … WHERE id=? AND status IN ('open','manager_responded')`; if 0 rows,
   tell the TL it was already closed.
4. Attachments → private Storage; content-type + size validated server-side; served to HRBP only via
   signed URLs with `Content-Disposition: attachment` (never inline).

### 6.4 HRBP dashboard (Supabase Auth + `hrbp_users` check)
1. Views: Open / Responded / Closed / Exceptions; filter by team, TL, department, date range.
2. Per responded case: see verdict + attachments → **Close as-is** (final = TL verdict) or
   **Override** (pick final status, mandatory comment).
3. Bulk close-as-is for uncontested confirms.
4. Exceptions queue: map an unknown status into `status_vocabulary`, or resolve an unmapped employee.

### 6.5 Period close (HRBP-managed, manual — no scheduler)
Cutoff timing is managed by HRBP, not the system. When HRBP decides the period is closing, a
dashboard action closes all remaining `open` cases in the range → `closed`,
`final_status = absent`, `closed_by = 'hrbp_cutoff'`. Optionally, HRBP first exports the list of
still-open cases to nudge TLs. No Scheduled Function; no SM escalation.

### 6.6 Report merge
Dashboard export: date range → CSV/Excel of `employee · work_date · source_status · final_status
(· final_leave_type)`. Join key back to the workbook: `CRM + work_date`. Merge stays outside the
system; source files untouched.

## 7. Security

- RLS on all tables; `anon` has zero direct access. All reads/writes go through Netlify Functions
  using the service key. Supabase keys never reach the TL page.
- TL token: high-entropy (32+ bytes base64url), **stored hashed**, checked server-side only.
  `Referrer-Policy: no-referrer` on the verify page; no third-party resources there; per-IP + per-token
  rate-limit on `/verify`. Threat model: a leaked token can only *excuse* absences, and the default is
  absence-stands — accepted.
- HRBP: Supabase Auth **and** `hrbp_users` allowlist check in every dashboard Function.
- Storage bucket private; attachments validated on upload, downloaded via signed URLs, forced as
  attachments (no inline render → no stored-XSS against HRBP session).

## 8. Edge Cases

- Employee changes TL mid-period → case keeps original `manager_id`; new cases use current mapping.
- Employee in Summary but not HC, or no TL in Structure → `ingestion_exceptions`, never dropped.
- TL re-submits a `manager_responded` case before HRBP closes → allowed, overwrites, audit-logged.
  After `closed` → rejected (optimistic lock).
- Date range overlapping an already-closed month → skip case creation, warn in summary.
- Unknown status string (new annotation, typo, date-serial leak) → exception, not a deduction.
- Consecutive same-employee absences → grouped in the TL UI with an "apply to selected rows" action.
- TL deactivated with open cases → HRBP reassigns via dashboard (cases keep audit history).

## 9. Build Order

1. Supabase schema + RLS + `status_vocabulary` seed (the ~70 known values) + `hrbp_users`.
2. Reference seed from `HC` + `Structure`; unmapped → exceptions.
3. Ingestion Function + `Summary Report` parser (wide→long, vocabulary lookup, exceptions).
4. TL verification page + token auth + multi-attachment submit + optimistic locking.
5. Email dispatch + `notifications` tracking + per-period reminder.
6. HRBP dashboard (lists, close, override, exceptions queue, export).
7. Manual period-close action + audit/exceptions views.

## 10. Open / Deferred

- iTalent direct integration (replaces Excel) — interface reserved, not built.
- `Late`, `Missing Punch Out`, `Half Day`, `2 Hour Excuse`, `Excuse` verification — deferred to a later stage.
- SM escalation — cancelled.
- Automated cutoff scheduler — not built; HRBP closes the period manually.
