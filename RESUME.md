# RESUME — project context for picking work back up

> Purpose: if this laptop is lost, `git clone` this repo on any machine, read this file, and a
> fresh Claude Code chat has enough context to continue. Last updated: 2026-07-23.

## What this project is

**51 Talk Attendance Verification** — a Streamlit app that ingests an attendance `Summary Report`
workbook into Supabase, then DMs each Team Leader (TL) a **private, unique link** to confirm their
team's flagged attendance days. HRBP dashboard, exceptions view, period-close, and private-bucket
attachment uploads are built. See `README.md`, `SPEC.md`, `DEPLOY.md` for detail.

Deployed on Streamlit Community Cloud (main file `streamlit_app.py`).

## Where everything lives

- **Code (durable):** GitHub `Hzmawadalla/abscense-verification`, branch `master`. This is the
  crash-proof copy — the local machine is disposable once pushed.
- **Secrets (NOT in git, must be re-entered on a new machine):** `.streamlit/secrets.toml`
  (see `.streamlit/secrets.toml.example`) locally, or the Streamlit Cloud Secrets manager in prod.
  Also `.env` and `config/tl_dingtalk.json` — all gitignored.
- **`gh` CLI is not authenticated** on this machine; plain `git push` over HTTPS works via the
  cached Git Credential Manager token. For PRs, use that token rather than `gh auth login`.

## Notification channels (two exist, same `send_link(...)` shape)

- `app/mailer.py` — SMTP email. Needs only `SMTP_HOST/PORT/USER/PASSWORD/MAIL_FROM`. **No DingTalk
  credentials.**
- `app/dingtalk.py` — DingTalk private work-notification DM.

## Open decision: DingTalk App Secret (as of 2026-07-23)

**Question:** can the DingTalk send avoid the App Secret (App Key + Agent ID only)?

**Answer:** No — for the private work-notification DM (`asyncsend_v2`), the **App Secret is 100%
mandatory**. `/gettoken` requires `appkey` + `appsecret` to mint the access token; the Agent ID only
routes the message and never authenticates. Confirmed in `app/dingtalk.py:53-64`.

**Ways to avoid it** (each changes the channel, not the config):
1. **Email via `app/mailer.py`** — already built, preserves the unique per-TL link, no App Secret.
   Recommended if the secret is a governance blocker.
2. **DingTalk group robot (webhook)** — auth is a signing secret (≠ App Secret), but posts to a
   shared group, which breaks the private per-TL-link model.
3. **Manual link distribution** — copy/paste from the HRBP dashboard, no credentials.

**Action pending:** an English message was drafted asking the China-based app admin for App Key,
App Secret, Agent ID, plus the "Work Notification (发工作通知)" send scope. Decide whether to wait
on the admin (keep DingTalk) or switch TL notifications to email now.

## Next steps if resuming

1. Recreate `.streamlit/secrets.toml` from the example + your saved secret values.
2. `python -m venv .venv && pip install -r requirements.txt` (Python may not be on PATH — check
   `python --version` first).
3. Decide DingTalk-vs-email for TL notifications (see open decision above).
