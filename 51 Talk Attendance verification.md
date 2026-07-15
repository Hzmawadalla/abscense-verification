# 51 Talk — Attendance Verification

Operational runbook for the HRBP running the monthly attendance-verification cycle.

The system parses the monthly attendance workbook, creates **verification cases** for
disputable days, lets each **Team Leader (TL)** confirm/correct their team's days via a unique
link, and lets **HRBP** close or override before results merge back into payroll.

- **App:** https://abscense-verification-51talk.streamlit.app
- **HRBP login:** email + password (Dashboard). **TL access:** unique `?t=<token>` link (no login).

---

## Monthly workflow (do it in this order)

1. **Prepare the files**
   - **Reference workbook** — the HR export with an **`HC`** tab and a **`Structure`** tab.
   - **Attendance report** — the attendance-tool export with a **`Summary Report`** tab.
   - *(A single workbook containing all three tabs also works — upload it in either box.)*

2. **Ingest** (⬆️ Ingest tab)
   - Upload the reference workbook and the attendance report.
   - Set the **Year** for the date columns.
   - **Parse & load.**

3. **Fix Exceptions FIRST** (⚠️ Exceptions tab)
   - Work the list — especially `unmapped_employee` (no TL in Structure).
   - Correct the Structure/HC and **re-ingest** so those employees get cases and the right TL.
   - ⚠️ Do this **before** sending links — see Critical Rule #1.

4. **Send links to TLs** (🔗 TL links tab) — pick one:
   - **📧 Email** (if SMTP configured) — bulk or per-TL.
   - **⬇️ Download all TL links (Excel)** — for Word Mail-Merge or manual send.
   - **Link** button — copy one link at a time.
   - Send **once** — see Critical Rule #2.

5. **TLs verify** — each opens their link and confirms every flagged day
   (Present / Annual Leave / Unpaid Leave / Sick Leave / Absent / Half Day). One submission per case.

6. **Resolve responded cases** (📋 Dashboard tab)
   - Review each TL verdict → **Close as-is** or **Override & close** (override needs a reason).

7. **Period close** (🔒 Period close tab) — *only when everything is verified.*
   - Stands every remaining open case as **Absent**. **Irreversible.**

8. **Export for payroll** (📤 Export tab)
   - Re-upload the period's attendance workbook → **Build reconciled report**.
   - Sheet 1 = matrix with closed cases overwritten by their final verdict.
   - Sheet 2 = "Changes" log (CRM · date · before → after · **In workbook?**).

---

## ⚠️ Things to take care about

### 🔴 Highest impact

1. **Fix TL mapping (Structure) BEFORE cases are created.**
   A case's TL owner is locked at creation and does **not** change on re-ingest — even if you
   later correct that employee's TL. Fixing it afterward only affects *new* cases. Work Exceptions
   and get mapping right first, then ingest.

2. **Links rotate every time you generate them — send once.**
   Clicking **Link**, **Email**, or **Prepare download** rotates that TL's token and
   **invalidates any link already sent**. Distribute once per round; don't re-prepare/re-email
   after sending, or the links in inboxes stop working.

3. **"Close the period" is irreversible.**
   It stands **every** remaining open case as **Absent**. Only click when everything is verified.

4. **Set the correct Year on ingest.**
   Date columns are parsed with the Year you pick. Wrong year → wrong dates on every case.

### 🟠 Data hygiene

5. **Everything accumulates; nothing is auto-cleaned.**
   The DB keeps cases from **every file ever ingested**. A corrected re-upload that *drops* a flag
   leaves the old case (and verdict) lingering. Clean test data before a real run; the reconciled
   export's **"In workbook?"** flag marks cases that belong to another file/period.

6. **Keep CRM formats consistent across files.**
   CRM is the join key across HC, Structure, and Summary Report. Mixing exports with different CRM
   styles → employees don't map → they land in Exceptions and get **no case** (skip verification).

7. **Unmapped employees slip through.**
   Employees with no TL mapping get **no case created** — they won't be verified. Work Exceptions
   or they're invisible to the process.

### 🟡 Verification & export behavior

8. **One-time TL validation — no self-corrections.**
   Each TL validates a case **once**, then it's locked. Mistakes are fixed only by **HR** (Dashboard
   override/close). Tell TLs to review before submitting.

9. **The reconciled export needs the matching workbook and shows only *closed* cases.**
   Re-upload the file for the period you're reconciling; cases not in that file show
   **"In workbook? No"**. Open/responded (not-yet-closed) cases won't appear.

### ⚙️ Config & security

10. **Rotate exposed keys** — the `SUPABASE_SERVICE_ROLE_KEY` and DB password were shared during
    setup; rotate them in Supabase and update Streamlit Secrets.
11. **Attachments need `SUPABASE_SERVICE_ROLE_KEY`** set, or upload is disabled.
12. **Free-tier sleep** — the app sleeps after inactivity; open it yourself once before a link blast
    so the first TL doesn't hit a ~1-minute wake-up.

---

## Reference

### Verdicts (TL & HRBP)
`Present` · `Annual Leave` · `Unpaid Leave` · `Sick Leave` · `Absent` · `Half Day`
> Half-day / sick-leave flow into the export as labels — the actual pay math is a manual
> downstream step.

### Required workbook tabs
| Tab | From | Used for |
|---|---|---|
| `HC` | HR export | employee master |
| `Structure` | HR export | employee → TL mapping |
| `Summary Report` | attendance tool | the day-by-day matrix |
> Tab names tolerate stray spaces and case (e.g. `Structure ` works).

### Streamlit Secrets (app config)
| Key | Purpose |
|---|---|
| `SUPABASE_DB_URL` | database connection (required) |
| `AUTH_COOKIE_KEY` | HRBP login sessions |
| `TOKEN_ENC_KEY` | Fernet key encrypting stored TL tokens (required to generate links) |
| `APP_BASE_URL` | base of the TL links — must be the real app URL |
| `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | case attachments |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `MAIL_FROM` / `MAIL_REPLY_TO` | email sending (optional) |
| `DINGTALK_APP_KEY` / `_APP_SECRET` / `_AGENT_ID` | DingTalk auto-send (optional) |

---

## Quick troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| "Worksheet X does not exist" | Missing/renamed tab — needs `HC` + `Structure` (reference) or `Summary Report` (attendance) |
| "no 'CRM' column found" | Wrong sheet uploaded, or it lacks a CRM column |
| TL sees "invalid or rotated link" | The link was regenerated after sending — send the newest one |
| Employee missing from verification | Unmapped (Exceptions) — fix Structure and re-ingest |
| Case in export "Changes" but not in the matrix | It belongs to a different file/period — **In workbook? No** |
| App slow / "connection error" on first open | Free-tier waking from sleep — wait ~1 min, refresh |
