# Design: Idempotent TL link generation (+ explicit rotate)

Date: 2026-07-14
Status: Approved

## Problem

TLs frequently hit *"This link is invalid or has been rotated. Please contact HR for a new
link."* A major avoidable cause is that **every** link action rotates the token:
`generate_manager_link` mints a fresh random token and overwrites the stored hash on each call.
Because only the token's **hash** is stored (never the raw token), the app cannot reproduce an
existing link — so "Download all TL links", re-emailing, or re-opening a link all rotate,
silently invalidating links already sitting in TLs' inboxes.

HRBP needs link actions to be **idempotent**: generating/downloading/copying a TL's link should
return the *same* still-valid link every time. A link should be invalidated only by a deliberate,
explicitly-labelled **Rotate** action.

## Approach

Store the raw token alongside its hash so the app can reproduce a link on demand (Approach A,
chosen over an HMAC-derived-token scheme for simplicity). These tokens are low-sensitivity bearer
capabilities — they already travel by email and rest in inboxes as plaintext, and unlock only a
TL's own attendance-verification list (names + flagged days, no financial data) — so storing the
raw token in the service-role-only DB is a small incremental risk.

## Components

### 1. Schema — migration `..._manager_access_token.sql`

Add a nullable column to `attendance.managers`:

```sql
alter table attendance.managers add column access_token text;
```

Additive and non-breaking; existing rows get `NULL`. The existing `access_token_hash` (unique)
remains the lookup key — no change to how a TL link is validated.

### 2. Data layer — `app/data.py`

- **`generate_manager_link(conn, manager_id) -> str`** becomes idempotent:
  - `SELECT access_token FROM attendance.managers WHERE id = %s`.
  - If a token exists, return it — **no write**.
  - Otherwise mint a token, `UPDATE ... SET access_token = raw, access_token_hash = hash(raw)`,
    commit, return the raw token.
- **`rotate_manager_link(conn, manager_id) -> str`** (new): always mint a fresh token, overwrite
  both `access_token` and `access_token_hash`, commit, return it. This is the *previous*
  `generate_manager_link` behavior, now reachable only through an explicit action.
- **`manager_by_token(conn, token)`** unchanged — still matches on `access_token_hash` and
  `active`. Storing the raw token does not change validation.

### 3. UI — `streamlit_app.py`

- The four existing call sites keep calling `generate_manager_link`, now idempotent, so they stop
  rotating:
  - email send (`send_tl_link`, ~:116)
  - DingTalk send (`send_tl_link`, ~:101)
  - "Download all TL links" loop (~:389)
  - per-manager link display/copy (~:407)
- Add a per-manager **"Rotate link"** button behind a confirmation, wired to
  `rotate_manager_link`.
- Reword the two warning captions (~:343, ~:400): the "this rotates the token and invalidates any
  link already sent" warning now attaches to the **Rotate** action, not to generate/download.

### 4. Loader — no change

`UPSERT_MANAGER` already leaves `access_token_hash` untouched on conflict and will not touch
`access_token` either, so an ordinary re-ingest of the same managers preserves their live links.

## One-time migration effect

The managers that currently hold only a hash (no stored raw token) cannot have their existing link
reproduced — the raw token was never stored. The **first** link action after deploy mints a fresh
token for each of them once (a single rotation), stable thereafter. This is acceptable: links are
already churned from recent data resets and the identity switch.

## Testing

- **Idempotency:** `generate_manager_link` called twice for a manager who already has a token
  returns the same token and performs no second write (assert via a fake connection recording
  executes).
- **Rotate:** `rotate_manager_link` returns a token different from the current one; afterward
  `manager_by_token(old)` is `None` and `manager_by_token(new)` resolves the manager.
- **Validation unchanged:** an issued token continues to validate via `manager_by_token`.
- **Re-ingest preserves links:** extend a loader test to assert `UPSERT_MANAGER` does not touch
  `access_token` / `access_token_hash`.
- Fake-connection unit tests mirror the existing data-layer test style; the full suite stays green.

## Scope (YAGNI)

Per-manager **Rotate** only — no bulk "rotate all" button unless later requested.

## Deployment

1. Add + commit the migration; apply it to the live Supabase DB (additive column, safe online).
2. Commit code changes; push to `master` → Community Cloud redeploys.
3. After deploy, the first "Download all TL links" mints tokens once for managers lacking a stored
   token; distribute those links — and from then on, re-downloading/re-emailing is safe and
   returns the same links.
