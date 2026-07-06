# Deployment Runbook

The system is a single **Streamlit** app backed by **Supabase** (Postgres `attendance` schema +
Storage), notifying TLs via **DingTalk**. Follow these steps once to go live.

## 1. Secrets

Copy `.streamlit/secrets.toml.example` → `.streamlit/secrets.toml` (local) or paste into the
Streamlit Community Cloud **Secrets** manager.

| Key | Where to get it | Needed for |
|---|---|---|
| `SUPABASE_DB_URL` | Supabase → Settings → Database → Connection string (URI, port 6543 pooler) | everything |
| `AUTH_COOKIE_KEY` | any long random string | HRBP login sessions |
| `APP_BASE_URL` | your deployed app URL, e.g. `https://xxx.streamlit.app` | building TL links |
| `SUPABASE_URL` | `https://twddavxodczywjshedqk.supabase.co` | attachments |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase → Settings → API → service_role (secret!) | attachments |
| `DINGTALK_APP_KEY` / `_APP_SECRET` / `_AGENT_ID` | your DingTalk corporate internal app | sending TL links |

## 2. First HRBP login

```bash
pip install -r requirements.txt
SUPABASE_DB_URL=postgresql://... python tools/create_hrbp.py --email you@51talk.com --name "You"
```

## 3. TL → DingTalk userids

Copy `config/tl_dingtalk.example.json` → `config/tl_dingtalk.json` and fill each TL's CRM → DingTalk
userid (from your DingTalk admin/contacts). Applied automatically on each ingestion.
Also fill `config/tl_aliases.json` for any TL missing from HC (e.g. `Zimmy`).

## 4. Run / deploy

```bash
streamlit run streamlit_app.py          # local check
```
Then deploy the repo on **Streamlit Community Cloud** (main file: `streamlit_app.py`), set the
secrets, and note the app URL back into `APP_BASE_URL`.

## 5. Monthly operation (HRBP)

1. **Ingest** tab → upload the attendance workbook, set the year → *Parse & load*.
2. **Exceptions** tab → work the list (esp. `unmapped_employee` → fix `Structure`, re-upload).
3. **TL links** tab → *Send to ALL with open cases* (DingTalk DMs each TL their link).
4. TLs open their link, confirm Present / On Leave / Absent (+ comment/attachment).
5. **Dashboard** tab → resolve responded cases (close as-is / override).
6. **Period close** tab → stand remaining open cases as Absent at cutoff.
7. Export closed cases (join back on `CRM + work_date`) into payroll.

## Verified vs. needs first-run check

- **Verified** (live DB + 58 tests): schema, RLS, ingestion→cases, loader SQL, token auth,
  verdict/close/audit, DingTalk & storage DB paths, all parsing/classification.
- **Confirm on first real run**: the Streamlit UI renders as intended; `streamlit-authenticator`
  login (API varies by version); a real DingTalk send against your app; a real file upload/download.
