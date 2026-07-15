# HRBP Upload Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give HRBP a front-end "Uploads" tab to remove a single bad upload's data (guarded when it holds verified verdicts) and to clear all case data for a fresh start — without touching reference data.

**Architecture:** Tag each case with the upload that created it (`cases.ingestion_run_id`). New data-layer functions list uploads with counts, remove one upload's cases + run, or reset all case data. A new Streamlit tab drives them behind confirmations.

**Tech Stack:** Python, psycopg (raw cursors + `dict_row`), Streamlit, Supabase/Postgres (`attendance` schema), pytest with hand-rolled fake connections.

## Global Constraints

- All SQL is schema-qualified to `attendance`.
- Deletions preserve `managers`, `employees`, `status_vocabulary`, `hrbp_users` (and TL tokens).
- `cases.ingestion_run_id` FK is `ON DELETE SET NULL`; removal is always explicit in app code.
- Creator-owns: the case→run link is never overwritten on re-ingest conflict.
- Full pytest suite must stay green (`.venv/Scripts/python.exe -m pytest -q`).
- Data-layer tests use fake connections (no live DB), mirroring `tests/test_manager_links.py`.
- **Do not deploy as part of this plan** unless explicitly told — Task 5's live-DB + push steps are gated.

---

### Task 1: Migration — `cases.ingestion_run_id` + backfill

**Files:**
- Create: `supabase/migrations/20260715120000_cases_ingestion_run_id.sql`

**Interfaces:**
- Produces: column `attendance.cases.ingestion_run_id uuid` (nullable, FK → `ingestion_runs`),
  backfilled for existing rows. Consumed by Tasks 2–3.

- [ ] **Step 1: Write the migration file**

`supabase/migrations/20260715120000_cases_ingestion_run_id.sql`:

```sql
-- Tag each case with the upload (ingestion run) that created it, so HRBP can remove a single
-- upload's cases from the UI. SET NULL (not cascade) so a run delete never silently drops cases.
set search_path = attendance, public;

alter table attendance.cases
  add column if not exists ingestion_run_id uuid
    references attendance.ingestion_runs(id) on delete set null;

create index if not exists cases_ingestion_run_id_idx on attendance.cases(ingestion_run_id);

-- One-time backfill: each existing case -> the run active when it was created (latest run whose
-- created_at is at or before the case's). Exact for single-run data; best-effort otherwise.
update attendance.cases c
set ingestion_run_id = (
  select r.id from attendance.ingestion_runs r
  where r.created_at <= c.created_at
  order by r.created_at desc
  limit 1)
where c.ingestion_run_id is null;
```

- [ ] **Step 2: Commit** (apply-to-live is deferred to Task 5)

```bash
git add supabase/migrations/20260715120000_cases_ingestion_run_id.sql
git commit -m "feat: add cases.ingestion_run_id to tag cases by upload"
```

---

### Task 2: Loader — tag cases with the run id

**Files:**
- Modify: `ingestion/loader.py` (`UPSERT_CASE`, `case_params`, `load_ingestion`)
- Test: `tests/test_loader.py` (update `test_case_params_shape` + the ordering test)

**Interfaces:**
- Consumes: `load_ingestion` already creates `run_id` via `INSERT_RUN`.
- Produces: `case_params(c, run_id)` returns a 6-tuple ending in `run_id`; `UPSERT_CASE` inserts
  `ingestion_run_id` and does NOT update it on conflict.

- [ ] **Step 1: Update the two affected tests**

In `tests/test_loader.py`, replace `test_case_params_shape` (currently ~:72-76):

```python
def test_case_params_shape(sample_workbook_with_summary):
    ref = parse_reference(sample_workbook_with_summary)
    res = ingest_summary(sample_workbook_with_summary, ref, year=2026)
    c = next(c for c in res.cases if c.employee_crm == "E-1")
    assert loader.case_params(c, "run-1") == ("E-1", "TL-A", c.work_date, "Absent", False, "run-1")


def test_upsert_case_does_not_reassign_owning_run_on_conflict():
    # Creator-owns: a re-ingest that re-touches a day must not steal the case's ingestion_run_id.
    assert "ingestion_run_id = excluded" not in loader.UPSERT_CASE.lower()
```

And add, inside `test_load_ingestion_records_run_then_cases_then_exceptions` (after the existing
assertions, ~:56):

```python
    # every case row carries the owning run id (last element)
    assert all(row[-1] == "run-xyz" for row in db.calls[1][2])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_loader.py -q`
Expected: FAIL — `case_params()` takes 1 arg, and case rows lack the run id.

- [ ] **Step 3: Implement the loader change**

In `ingestion/loader.py`, replace `UPSERT_CASE` (currently ~:45-55):

```python
UPSERT_CASE = """
insert into attendance.cases
  (employee_id, manager_id, work_date, source_status, is_half_day, ingestion_run_id)
values
  ((select id from attendance.employees where crm = %s),
   (select id from attendance.managers where crm = %s),
   %s, %s, %s, %s)
on conflict (employee_id, work_date) do update set
  source_status = excluded.source_status,
  is_half_day = excluded.is_half_day
"""
```

Replace `case_params` (currently ~:79-80):

```python
def case_params(c, run_id):
    return (c.employee_crm, c.manager_crm, c.work_date, c.source_status, c.is_half_day, run_id)
```

In `load_ingestion`, replace the case-loading line (currently ~:108):

```python
    db.many(UPSERT_CASE, [case_params(c, run_id) for c in result.cases])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_loader.py -q`
Expected: PASS (all loader tests green)

- [ ] **Step 5: Commit**

```bash
git add ingestion/loader.py tests/test_loader.py
git commit -m "feat: tag ingested cases with their ingestion_run_id (creator-owns)"
```

---

### Task 3: Data layer — list / remove / reset

**Files:**
- Modify: `app/data.py` (add `list_uploads`, `remove_upload`, `reset_all_cases`)
- Test: `tests/test_uploads.py` (create)

**Interfaces:**
- Consumes: `dict_row` (already imported in `app/data.py`).
- Produces:
  - `list_uploads(conn) -> list[dict]` — `{id, source_filename, created_at, total, verified, open}`, newest first.
  - `remove_upload(conn, run_id) -> dict` — `{cases_deleted, verified_deleted}`.
  - `reset_all_cases(conn) -> dict` — `{cases_deleted}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_uploads.py`:

```python
"""Behavior contract for HRBP upload management: list, remove-one, reset-all."""
from app import data


class FakeCursor:
    """psycopg-like cursor over an in-memory {run_id: [case,...]} store, where a case is a dict
    {'verified': bool, 'open': bool}. Only the queries these functions issue are modelled."""

    def __init__(self, store, calls):
        self.store, self.calls = store, calls
        self._one = None
        self._all = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.lower().split())
        self.calls.append(s)
        if s.startswith("select r.id, r.source_filename"):
            self._all = [
                {"id": rid, "source_filename": f"{rid}.xlsx", "created_at": None,
                 "total": len(cs),
                 "verified": sum(1 for x in cs if x["verified"]),
                 "open": sum(1 for x in cs if x["open"])}
                for rid, cs in self.store.items()]
        elif s.startswith("select count(*), count(*) filter") and "ingestion_run_id = %s" in s:
            cs = self.store.get(params[0], [])
            self._one = (len(cs), sum(1 for x in cs if x["verified"]))
        elif s.startswith("delete from attendance.cases where ingestion_run_id = %s"):
            self.store.pop(params[0], None)
        elif s.startswith("delete from attendance.ingestion_runs where id = %s"):
            pass
        elif s.startswith("select count(*) from attendance.cases"):
            self._one = (sum(len(cs) for cs in self.store.values()),)
        elif s.startswith("delete from attendance.cases"):
            self.store.clear()
        elif s.startswith("delete from attendance.ingestion_exceptions"):
            pass
        elif s.startswith("delete from attendance.ingestion_runs"):
            pass

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class FakeConn:
    def __init__(self, store):
        self.store, self.calls, self.committed = store, [], 0

    def cursor(self, row_factory=None):
        return FakeCursor(self.store, self.calls)

    def commit(self):
        self.committed += 1


def _store():
    return {
        "A": [{"verified": True, "open": False}, {"verified": False, "open": True}],
        "B": [{"verified": False, "open": True}, {"verified": False, "open": True},
              {"verified": True, "open": False}],
    }


def test_list_uploads_reports_counts_per_run():
    conn = FakeConn(_store())
    rows = {r["id"]: r for r in data.list_uploads(conn)}
    assert rows["A"]["total"] == 2 and rows["A"]["verified"] == 1 and rows["A"]["open"] == 1
    assert rows["B"]["total"] == 3 and rows["B"]["verified"] == 1


def test_remove_upload_deletes_only_that_run_and_reports_counts():
    conn = FakeConn(_store())
    res = data.remove_upload(conn, "A")
    assert res == {"cases_deleted": 2, "verified_deleted": 1}
    assert "A" not in conn.store          # A's cases gone
    assert len(conn.store["B"]) == 3      # B untouched
    assert conn.committed == 1


def test_reset_all_cases_clears_everything_but_not_managers():
    conn = FakeConn(_store())
    res = data.reset_all_cases(conn)
    assert res == {"cases_deleted": 5}
    assert conn.store == {}
    joined = " | ".join(conn.calls)
    assert "delete from attendance.cases" in joined
    assert "delete from attendance.ingestion_exceptions" in joined
    assert "delete from attendance.ingestion_runs" in joined
    assert "managers" not in joined       # reference data is never touched
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_uploads.py -q`
Expected: FAIL — `list_uploads` / `remove_upload` / `reset_all_cases` don't exist.

- [ ] **Step 3: Implement the functions**

Append to `app/data.py`:

```python
# --------------------------------------------------------------------------- upload management
def list_uploads(conn):
    """Every ingestion run with its case counts (newest first), for the HRBP Uploads panel."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select r.id, r.source_filename, r.created_at, "
            "       count(c.id) as total, "
            "       count(c.id) filter (where c.manager_status is not null) as verified, "
            "       count(c.id) filter (where c.status = 'open') as open "
            "from attendance.ingestion_runs r "
            "left join attendance.cases c on c.ingestion_run_id = r.id "
            "group by r.id, r.source_filename, r.created_at "
            "order by r.created_at desc")
        return cur.fetchall()


def remove_upload(conn, run_id) -> dict:
    """Delete one upload: its cases (by ingestion_run_id) then the run (exceptions cascade).
    Returns how many cases — and how many of them verified — were removed."""
    with conn.cursor() as cur:
        cur.execute("select count(*), count(*) filter (where manager_status is not null) "
                    "from attendance.cases where ingestion_run_id = %s", (run_id,))
        total, verified = cur.fetchone()
        cur.execute("delete from attendance.cases where ingestion_run_id = %s", (run_id,))
        cur.execute("delete from attendance.ingestion_runs where id = %s", (run_id,))
    conn.commit()
    return {"cases_deleted": total, "verified_deleted": verified}


def reset_all_cases(conn) -> dict:
    """Clear all case data (cases, ingestion exceptions, ingestion runs) for a fresh start.
    Managers, employees, TL tokens, status vocabulary, and HRBP logins are preserved."""
    with conn.cursor() as cur:
        cur.execute("select count(*) from attendance.cases")
        n = cur.fetchone()[0]
        cur.execute("delete from attendance.cases")
        cur.execute("delete from attendance.ingestion_exceptions")
        cur.execute("delete from attendance.ingestion_runs")
    conn.commit()
    return {"cases_deleted": n}
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_uploads.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add app/data.py tests/test_uploads.py
git commit -m "feat: list_uploads / remove_upload / reset_all_cases data-layer functions"
```

---

### Task 4: UI — the Uploads tab

**Files:**
- Modify: `streamlit_app.py` (tabs line ~:242-243; add a `with tab_uploads:` block)

**Interfaces:**
- Consumes: `data.list_uploads`, `data.remove_upload`, `data.reset_all_cases`.

- [ ] **Step 1: Add the tab to the tab list**

Replace (~:242-243):

```python
    tab_dash, tab_ingest, tab_exc, tab_links, tab_close, tab_export = st.tabs(
        ["📋 Dashboard", "⬆️ Ingest", "⚠️ Exceptions", "🔗 TL links", "🔒 Period close", "📤 Export"])
```

with:

```python
    tab_dash, tab_ingest, tab_exc, tab_links, tab_close, tab_export, tab_uploads = st.tabs(
        ["📋 Dashboard", "⬆️ Ingest", "⚠️ Exceptions", "🔗 TL links", "🔒 Period close",
         "📤 Export", "🗂️ Uploads"])
```

- [ ] **Step 2: Add the Uploads tab block**

Immediately after the `with tab_export:` block ends (find the last line of that block, before
`render_hrbp` returns), add:

```python
    with tab_uploads:
        st.write("Every attendance upload and its cases. Remove a bad upload, or clear everything to "
                 "start fresh. Reference data (managers, employees, links, vocabulary, login) is never "
                 "touched here.")
        uploads = data.list_uploads(c)
        if not uploads:
            st.info("No uploads yet.")
        for up in uploads:
            cols = st.columns([4, 1, 1, 1, 2])
            when = up["created_at"].strftime("%Y-%m-%d %H:%M") if up["created_at"] else "—"
            cols[0].write(f"**{up['source_filename'] or '—'}**  \n{when}")
            cols[1].metric("cases", up["total"])
            cols[2].metric("verified", up["verified"])
            cols[3].metric("open", up["open"])
            with cols[4]:
                if up["verified"]:
                    ack = st.checkbox(f"⚠️ delete {up['verified']} verified verdict(s)",
                                      key=f"ack{up['id']}")
                    typed = st.text_input("type REMOVE", key=f"rm{up['id']}",
                                          label_visibility="collapsed", placeholder="type REMOVE")
                    ready = ack and typed.strip() == "REMOVE"
                else:
                    ready = st.checkbox("confirm remove", key=f"ack{up['id']}")
                if st.button("Remove upload", key=f"rmbtn{up['id']}", disabled=not ready):
                    res = data.remove_upload(c, up["id"])
                    st.success(f"Removed {res['cases_deleted']} case(s) "
                               f"({res['verified_deleted']} verified).")
                    st.rerun()

        st.divider()
        st.error("⚠️ **Clear everything & start fresh** — deletes ALL cases, exceptions, and uploads "
                 "(keeps managers, employees, links, vocabulary, login). Cannot be undone.")
        typed_all = st.text_input("To confirm, type  CLEAR  in capitals:", key="reset_all")
        if st.button("Clear all case data", type="primary", disabled=(typed_all.strip() != "CLEAR")):
            res = data.reset_all_cases(c)
            st.success(f"Cleared {res['cases_deleted']} case(s). Start fresh with a new ingest.")
            st.rerun()
```

- [ ] **Step 3: Verify the file parses and the suite is green**

Run:
```bash
.venv/Scripts/python.exe -c "import ast; ast.parse(open('streamlit_app.py',encoding='utf-8').read()); print('OK')"
.venv/Scripts/python.exe -m pytest -q
```
Expected: `OK` and full suite passes.

- [ ] **Step 4: Manual smoke (local run)**

Run: `.venv/Scripts/streamlit.exe run streamlit_app.py` (needs `SUPABASE_DB_URL` + `TOKEN_ENC_KEY`),
log in, open **🗂️ Uploads**:
- The current upload(s) appear with case/verified/open counts.
- On an upload with verified cases, the **Remove upload** button stays disabled until you tick the
  acknowledgement AND type `REMOVE`.
- The **Clear all case data** button stays disabled until you type `CLEAR`.
(Do not actually delete unless you intend to.) Stop the app when done.

- [ ] **Step 5: Commit**

```bash
git add streamlit_app.py
git commit -m "feat: HRBP Uploads tab — remove one upload (guarded) or clear all case data"
```

---

### Task 5: Verification + deploy (GATED — only on explicit go-ahead)

**Files:** none.

- [ ] **Step 1: Full suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: all tests pass (existing + new loader/upload tests).

- [ ] **Step 2: Apply the migration to the live Supabase DB** (additive column + index + backfill)

```bash
.venv/Scripts/python.exe - <<'PY'
import psycopg
url=[l.split("=",1)[1].strip() for l in open(".env",encoding="utf-8")
     if l.strip().startswith("SUPABASE_DB_URL=")][0]
sql=open("supabase/migrations/20260715120000_cases_ingestion_run_id.sql",encoding="utf-8").read()
with psycopg.connect(url) as c, c.cursor() as cur:
    cur.execute(sql); c.commit()
    cur.execute("select count(*) from attendance.cases where ingestion_run_id is not null")
    print("cases tagged:", cur.fetchone()[0])
PY
```
Expected: `cases tagged: <n>` (n = current case count, all backfilled).

- [ ] **Step 3: Push** (triggers production Streamlit redeploy)

```bash
git push origin master
```

- [ ] **Step 4: Post-deploy check** — open **🗂️ Uploads**, confirm the upload list + counts render, and
  that Remove/Clear stay gated behind their confirmations.

---

## Notes

- Links are intentionally untouched by this feature — a TL token belongs to a manager, not an upload.
- `remove_upload` relies on `ingestion_run_id`; cases predating the backfill that couldn't be matched
  (none expected) would show under no upload and are only clearable via **Clear all case data**.
