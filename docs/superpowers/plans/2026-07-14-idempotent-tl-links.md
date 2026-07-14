# Idempotent TL Link Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make TL link generation idempotent — copying/emailing/downloading a link returns the same still-valid link — with a deliberate, confirmed "Rotate" the only action that invalidates a link.

**Architecture:** Store the raw token in a new `attendance.managers.access_token` column next to the existing `access_token_hash`. `generate_manager_link` returns the stored token if present, else mints one; a new `rotate_manager_link` is the only path that overwrites it. Link validation (`manager_by_token`) is unchanged — it still matches on the hash.

**Tech Stack:** Python, psycopg (raw cursors), Streamlit, Supabase/Postgres (`attendance` schema), pytest with hand-rolled fake connections.

## Global Constraints

- All SQL is schema-qualified to `attendance` (e.g. `attendance.managers`).
- Only the token's hash is used for lookup; the raw token is stored solely to reproduce links.
- Rotation is per-manager and explicit — no bulk "rotate all" (YAGNI).
- `manager_by_token` behavior is unchanged; do not alter its query.
- The full pytest suite must stay green (`.venv/Scripts/python.exe -m pytest -q`).
- Data-layer tests use fake connections (no live DB), mirroring `tests/test_submit_verdict.py`.

---

### Task 1: Migration — add `access_token` column

**Files:**
- Create: `supabase/migrations/20260714120000_attendance_manager_access_token.sql`

**Interfaces:**
- Produces: column `attendance.managers.access_token text` (nullable), consumed by Task 2.

- [ ] **Step 1: Write the migration file**

`supabase/migrations/20260714120000_attendance_manager_access_token.sql`:

```sql
-- Store the raw TL access token alongside its hash so a link can be reproduced on demand,
-- making link generation idempotent (only an explicit rotate invalidates a link).
set search_path = attendance, public;

alter table attendance.managers
  add column if not exists access_token text;
```

- [ ] **Step 2: Apply it to the live Supabase database**

The column is additive and unused until Task 2's code deploys, so applying now is safe.

Run (from repo root, reads `SUPABASE_DB_URL` from `.env`):

```bash
.venv/Scripts/python.exe - <<'PY'
import psycopg
url=[l.split("=",1)[1].strip() for l in open(".env",encoding="utf-8")
     if l.strip().startswith("SUPABASE_DB_URL=")][0]
with psycopg.connect(url) as c, c.cursor() as cur:
    cur.execute("alter table attendance.managers add column if not exists access_token text")
    c.commit()
    cur.execute("select column_name from information_schema.columns "
                "where table_schema='attendance' and table_name='managers' and column_name='access_token'")
    print("access_token present:", cur.fetchone() is not None)
PY
```

Expected: `access_token present: True`

- [ ] **Step 3: Commit**

```bash
git add supabase/migrations/20260714120000_attendance_manager_access_token.sql
git commit -m "feat: add managers.access_token column for reproducible TL links"
```

---

### Task 2: Data layer — idempotent generate + explicit rotate

**Files:**
- Modify: `app/data.py` (replace `generate_manager_link`, add `rotate_manager_link`)
- Test: `tests/test_manager_links.py` (create)

**Interfaces:**
- Consumes: `security.generate_token()`, `security.hash_token(token)`, `data.manager_by_token(conn, token)` (unchanged).
- Produces:
  - `generate_manager_link(conn, manager_id) -> str` — idempotent; returns existing token or mints+stores one.
  - `rotate_manager_link(conn, manager_id) -> str` — always mints, overwrites `access_token` + `access_token_hash`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_manager_links.py`:

```python
"""Behavior contract for TL link tokens: generation is idempotent; rotation is explicit."""
from app import data, security


class FakeCursor:
    """psycopg-like cursor over an in-memory {manager_id: row} store."""

    def __init__(self, store, calls):
        self.store, self.calls = store, calls
        self._fetch = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.lower().split())
        self.calls.append(s.split()[0])  # 'select' / 'update'
        if s.startswith("select access_token from attendance.managers"):
            row = self.store.get(params[0])
            self._fetch = (row.get("access_token"),) if row else None
        elif s.startswith("update attendance.managers set access_token"):
            token, token_hash, mid = params
            self.store.setdefault(mid, {}).update(access_token=token, access_token_hash=token_hash)
        elif s.startswith("select id, crm, name, email, access_token_hash, active"):
            want = params[0]
            self._fetch = next(
                ({"id": mid, "crm": r.get("crm"), "name": r.get("name"), "email": r.get("email"),
                  "access_token_hash": r.get("access_token_hash"), "active": True}
                 for mid, r in self.store.items()
                 if r.get("access_token_hash") == want and r.get("active", True)),
                None)

    def fetchone(self):
        return self._fetch


class FakeConn:
    def __init__(self, store):
        self.store, self.calls, self.committed = store, [], 0

    def cursor(self, row_factory=None):
        return FakeCursor(self.store, self.calls)

    def commit(self):
        self.committed += 1


def _mgr():
    return {"access_token": None, "access_token_hash": None, "active": True}


def test_generate_mints_and_stores_a_token_when_none_exists():
    conn = FakeConn({"m1": _mgr()})
    tok = data.generate_manager_link(conn, "m1")
    assert tok
    assert conn.store["m1"]["access_token"] == tok
    assert conn.store["m1"]["access_token_hash"] == security.hash_token(tok)


def test_generate_is_idempotent_returns_same_token_without_rewriting():
    conn = FakeConn({"m1": _mgr()})
    first = data.generate_manager_link(conn, "m1")
    updates_after_first = conn.calls.count("update")
    second = data.generate_manager_link(conn, "m1")
    assert second == first
    assert conn.calls.count("update") == updates_after_first  # repeat call performs no write


def test_rotate_replaces_token_and_invalidates_the_old_link():
    conn = FakeConn({"m1": _mgr()})
    old = data.generate_manager_link(conn, "m1")
    new = data.rotate_manager_link(conn, "m1")
    assert new != old
    assert data.manager_by_token(conn, old) is None
    assert data.manager_by_token(conn, new)["id"] == "m1"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_manager_links.py -q`
Expected: FAIL — `rotate_manager_link` does not exist / idempotency assertion fails (old `generate_manager_link` always writes).

- [ ] **Step 3: Replace `generate_manager_link` and add `rotate_manager_link`**

In `app/data.py`, replace the existing `generate_manager_link` (currently at ~:57-64) with:

```python
def generate_manager_link(conn, manager_id) -> str:
    """Return the manager's existing TL link token, minting and storing one only if none exists.
    Idempotent: repeat calls (copy, download-all, re-email) return the same token, so links already
    distributed keep working. Use rotate_manager_link to deliberately invalidate and replace."""
    with conn.cursor() as cur:
        cur.execute("select access_token from attendance.managers where id = %s", (manager_id,))
        row = cur.fetchone()
        if row and row[0]:
            return row[0]
        token = security.generate_token()
        cur.execute("update attendance.managers set access_token = %s, access_token_hash = %s "
                    "where id = %s", (token, security.hash_token(token), manager_id))
    conn.commit()
    return token


def rotate_manager_link(conn, manager_id) -> str:
    """Mint a brand-new TL token, overwriting any existing one — this INVALIDATES the link already
    sent. Deliberate rotation only (the pre-idempotency generate_manager_link behavior)."""
    token = security.generate_token()
    with conn.cursor() as cur:
        cur.execute("update attendance.managers set access_token = %s, access_token_hash = %s "
                    "where id = %s", (token, security.hash_token(token), manager_id))
    conn.commit()
    return token
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_manager_links.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add app/data.py tests/test_manager_links.py
git commit -m "feat: idempotent generate_manager_link + explicit rotate_manager_link"
```

---

### Task 3: Loader guard — re-ingest must not touch the token

**Files:**
- Test: `tests/test_loader.py` (add one test)

**Interfaces:**
- Consumes: `loader.UPSERT_MANAGER` (SQL constant, unchanged).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_loader.py`:

```python
def test_upsert_manager_does_not_touch_link_token():
    # A plain re-ingest must preserve a manager's issued link: the manager upsert may not write
    # access_token or access_token_hash on conflict, or every re-ingest would invalidate links.
    assert "access_token" not in loader.UPSERT_MANAGER.lower()
```

- [ ] **Step 2: Run the test**

Run: `.venv/Scripts/python.exe -m pytest tests/test_loader.py::test_upsert_manager_does_not_touch_link_token -q`
Expected: PASS immediately — `UPSERT_MANAGER` already only updates `name`, `email`, `active`. (This test is a regression guard; if it fails, do NOT add access_token to the upsert.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_loader.py
git commit -m "test: guard that re-ingest preserves TL link tokens"
```

---

### Task 4: UI — Rotate button + reword captions

**Files:**
- Modify: `streamlit_app.py` (TL links tab: captions ~:342-343 and ~:399-400; per-manager row ~:402-414)

**Interfaces:**
- Consumes: `data.generate_manager_link` (idempotent), `data.rotate_manager_link` (new).

- [ ] **Step 1: Reword the "send once" caption**

In `streamlit_app.py`, replace (~:342-343):

```python
        st.caption("⚠️ Send each link **once**. Generating a new link — or re-preparing the download — "
                   "rotates that TL's token and invalidates any link you already sent.")
```

with:

```python
        st.caption("🔗 Links are stable — copying, e-mailing, or re-downloading returns the **same** "
                   "link. Only a TL's **Rotate** button issues a new link (and kills the old one).")
```

- [ ] **Step 2: Reword the download caption**

Replace (~:399-400):

```python
            st.caption("These are now the current valid links — distribute this file; preparing again "
                       "rotates the tokens and invalidates them.")
```

with:

```python
            st.caption("These are the current valid links — preparing again returns the **same** links, "
                       "so re-downloading is safe.")
```

- [ ] **Step 3: Add a per-manager Rotate control (confirm-gated)**

Replace the per-manager loop (~:402-414):

```python
        for mgr in overview:
            cols = st.columns([4, 1, 1, 3])
            email = mgr.get("email") or "— no email —"
            cols[0].write(f"**{mgr['name'] or mgr['crm']}** · {mgr['open_cases']} open · {email}")
            if cols[1].button("Link", key=f"gl{mgr['id']}"):
                tok = data.generate_manager_link(c, mgr["id"])
                st.session_state[f"link{mgr['id']}"] = f"{base}/?t={tok}"
            can_email = bool(mailer and mgr.get("email") and mgr["open_cases"])
            if cols[2].button("Email", key=f"eml{mgr['id']}", disabled=not can_email):
                ok, err = send_tl_email(c, mailer, mgr, base)
                st.success(f"Emailed {mgr['name']}.") if ok else st.error(f"Failed: {err}")
            if st.session_state.get(f"link{mgr['id']}"):
                cols[3].code(st.session_state[f"link{mgr['id']}"], language=None)
```

with:

```python
        for mgr in overview:
            cols = st.columns([4, 1, 1, 1, 3])
            email = mgr.get("email") or "— no email —"
            cols[0].write(f"**{mgr['name'] or mgr['crm']}** · {mgr['open_cases']} open · {email}")
            if cols[1].button("Link", key=f"gl{mgr['id']}"):
                tok = data.generate_manager_link(c, mgr["id"])
                st.session_state[f"link{mgr['id']}"] = f"{base}/?t={tok}"
            can_email = bool(mailer and mgr.get("email") and mgr["open_cases"])
            if cols[2].button("Email", key=f"eml{mgr['id']}", disabled=not can_email):
                ok, err = send_tl_email(c, mailer, mgr, base)
                st.success(f"Emailed {mgr['name']}.") if ok else st.error(f"Failed: {err}")
            # Rotate is the ONLY action that invalidates an existing link — gated by a confirm box.
            confirm = cols[3].checkbox("↻?", key=f"rotok{mgr['id']}",
                                       help="Rotate = issue a new link and invalidate the current one")
            if cols[3].button("Rotate", key=f"rot{mgr['id']}", disabled=not confirm):
                tok = data.rotate_manager_link(c, mgr["id"])
                st.session_state[f"link{mgr['id']}"] = f"{base}/?t={tok}"
                st.warning(f"Rotated {mgr['name'] or mgr['crm']}'s link — the previous link no longer works.")
            if st.session_state.get(f"link{mgr['id']}"):
                cols[4].code(st.session_state[f"link{mgr['id']}"], language=None)
```

- [ ] **Step 4: Verify the file parses and the suite is green**

Run:
```bash
.venv/Scripts/python.exe -c "import ast; ast.parse(open('streamlit_app.py',encoding='utf-8').read()); print('streamlit_app.py OK')"
.venv/Scripts/python.exe -m pytest -q
```
Expected: `streamlit_app.py OK` and the full suite passes.

- [ ] **Step 5: Manual smoke (local run)**

Run: `.venv/Scripts/streamlit.exe run streamlit_app.py` (needs `SUPABASE_DB_URL`), log in, open **TL links**:
- Click a TL's **Link** twice → the displayed link is identical both times.
- Tick **↻?** then **Rotate** → the link changes; the warning appears.
Stop the app when done.

- [ ] **Step 6: Commit**

```bash
git add streamlit_app.py
git commit -m "feat: per-TL Rotate button + reword link captions (links now stable)"
```

---

### Task 5: Full verification + deploy

**Files:** none (verification + release).

- [ ] **Step 1: Run the whole suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: all tests pass (existing count + 3 new in `test_manager_links.py` + 1 in `test_loader.py`).

- [ ] **Step 2: Confirm the migration is live** (from Task 1, Step 2 — re-run the check if unsure)

```bash
.venv/Scripts/python.exe - <<'PY'
import psycopg
url=[l.split("=",1)[1].strip() for l in open(".env",encoding="utf-8")
     if l.strip().startswith("SUPABASE_DB_URL=")][0]
with psycopg.connect(url) as c, c.cursor() as cur:
    cur.execute("select 1 from information_schema.columns where table_schema='attendance' "
                "and table_name='managers' and column_name='access_token'")
    print("column live:", cur.fetchone() is not None)
PY
```
Expected: `column live: True` (must be True before the new code deploys, or `generate_manager_link` errors on the missing column).

- [ ] **Step 3: Push to deploy** (USER-GATED — triggers a production Streamlit redeploy)

```bash
git push origin master
```

- [ ] **Step 4: Post-deploy check**

After Community Cloud redeploys (~2-3 min), in the app open **TL links**, click a TL's **Link** twice → identical link. Then generate/download links for the round and distribute once.

---

## Notes / one-time effect

Managers that currently hold only a hash (no stored raw token) cannot have their old link
reproduced — the raw token was never stored. The first **Link/Download** action per such manager
after deploy mints a fresh token once (a single rotation), stable thereafter.
