# UI Visual Direction — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the approved 51 Talk dark visual direction and plain-language vocabulary to the Streamlit app, as display-only changes plus one flagged behaviour change (the TL "Select…" verdict default).

**Architecture:** Two new pure modules — `app/labels.py` (display strings + one validation helper) and `app/branding.py` (CSS constant + HTML-returning UI helpers) — are unit-tested in the existing fake-cursor style. `streamlit_app.py` then imports them and is re-wired to inject the theme CSS, relabel statuses/tabs, and render the branded components. A new `.streamlit/config.toml` carries the dark theme.

**Tech Stack:** Python 3, Streamlit, pytest. No new dependencies.

**Design source:** `docs/UI-VISUAL-DIRECTION.md`. Mockup: https://claude.ai/code/artifact/d1171da6-effb-4a1b-85c4-4e2ad2e4292c

## Global Constraints

- **Committed dark** — `base="dark"`; this is a single-world design, not a toggle.
- **Brand hexes, one role each** — Blue `#0063F2` (actions; button fills use lifted `#1F74FF`), Yellow `#FFDE17` (signature only), Sky `#24A9FF` (info/on-leave), Toki `#FFB414` (awaiting), Font Gray `#3E3A39` (surfaces). Ground `#201F1D`.
- **Display-only relabels** — stored enum codes (`open`, `manager_responded`, `closed`, verdict codes) MUST NOT change. Do not touch the DB schema, ingestion, dispatch, or the reconciled-export logic.
- **One behaviour change only** — the TL "Select…" default (Task 6). Nothing else alters what a user must do.
- **Windows env** — Python may not be on PATH; check `python --version` first, fall back to `py`. Open files `encoding='utf-8'`; keep console output ASCII-only.
- **Follow existing test style** — pure functions, no Streamlit in tests (`from app import labels` / `branding`).

---

### Task 1: `app/labels.py` — display strings + verdict validation

**Files:**
- Create: `app/labels.py`
- Test: `tests/test_labels.py`

**Interfaces:**
- Produces:
  - `VERDICTS: dict[str, str]` — label → stored code (canonical; moved here from `streamlit_app.py`)
  - `VERDICT_LABEL: dict[str, str]` — code → label (reverse + legacy `"leave"`)
  - `STATUS_LABELS: dict[str, str]` — status code → human word
  - `SELECT_PLACEHOLDER: str` — the "no verdict chosen" sentinel
  - `status_label(code: str) -> str`
  - `missing_verdicts(selections: dict[int, str]) -> list[int]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_labels.py
"""Display-string and verdict-validation contract (pure, no Streamlit)."""
from app import labels


def test_status_label_maps_codes_to_human_words():
    assert labels.status_label("open") == "Awaiting confirmation"
    assert labels.status_label("manager_responded") == "Confirmed"
    assert labels.status_label("closed") == "Finalized"


def test_status_label_falls_back_to_raw_code_when_unknown():
    assert labels.status_label("weird_code") == "weird_code"


def test_verdicts_roundtrip_and_legacy_leave():
    assert labels.VERDICTS["Present"] == "present"
    assert labels.VERDICT_LABEL["present"] == "Present"
    assert labels.VERDICT_LABEL["leave"] == "On Leave"


def test_missing_verdicts_flags_only_the_placeholder():
    sel = {1: "Present", 2: labels.SELECT_PLACEHOLDER, 3: "Absent"}
    assert labels.missing_verdicts(sel) == [2]


def test_missing_verdicts_empty_when_all_chosen():
    assert labels.missing_verdicts({1: "Present", 2: "Sick Leave"}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Check the interpreter first, then run:

```bash
python --version || py --version
python -m pytest tests/test_labels.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.labels'`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/labels.py
"""Display-only labels and the verdict-selection guard (SPEC: UI-VISUAL-DIRECTION §7).

Stored enum codes are unchanged — these maps only decide what a human reads.
"""

# Verdict dropdown: label -> stored enum code (canonical home; imported by streamlit_app).
VERDICTS = {
    "Present": "present",
    "Annual Leave": "annual_leave",
    "Unpaid Leave": "unpaid_leave",
    "Sick Leave": "sick_leave",
    "Absent": "absent",
    "Half Day": "half_day",
}
VERDICT_LABEL = {code: label for label, code in VERDICTS.items()}
VERDICT_LABEL["leave"] = "On Leave"  # legacy rows

# Case-status code -> human word (display only).
STATUS_LABELS = {
    "open": "Awaiting confirmation",
    "manager_responded": "Confirmed",
    "closed": "Finalized",
}

# Sentinel shown first in the TL verdict dropdown so a verdict is an explicit choice.
SELECT_PLACEHOLDER = "Select…"  # "Select…"


def status_label(code: str) -> str:
    """Human word for a case-status code; unknown codes fall back to themselves."""
    return STATUS_LABELS.get(code, code)


def missing_verdicts(selections: dict) -> list:
    """Case ids whose verdict is still the placeholder (not yet chosen)."""
    return [cid for cid, label in selections.items() if label == SELECT_PLACEHOLDER]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_labels.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add app/labels.py tests/test_labels.py
git commit -m "feat: add app/labels for display strings and verdict guard"
```

---

### Task 2: `app/branding.py` — CSS constant + HTML UI helpers

**Files:**
- Create: `app/branding.py`
- Test: `tests/test_branding.py`

**Interfaces:**
- Consumes: `app.labels.status_label`, `STATUS_LABELS`
- Produces:
  - `BRAND_CSS: str` — one `<style>` block (brand stripe, pills, tiles, attention stripe, ring, focus)
  - `status_pill(code: str, suffix: str | None = None) -> str` — `<span class="pill p-…">…</span>`
  - `progress_ring(pct: int, caption: str) -> str`
  - `brand_stripe() -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_branding.py
"""Pure HTML-builder contract for the branded components (no Streamlit)."""
from app import branding


def test_status_pill_uses_semantic_class_and_human_label():
    html = branding.status_pill("open")
    assert "p-warn" in html
    assert "Awaiting confirmation" in html


def test_status_pill_appends_suffix():
    html = branding.status_pill("manager_responded", suffix="Annual Leave")
    assert "p-good" in html
    assert "Confirmed · Annual Leave" in html  # "Confirmed · Annual Leave"


def test_status_pill_escapes_untrusted_suffix():
    html = branding.status_pill("closed", suffix="<script>x</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_progress_ring_clamps_and_shows_caption():
    html = branding.progress_ring(150, "12 / 18 teams")
    assert "--p:100" in html          # clamped to 100
    assert "100%" in html
    assert "12 / 18 teams" in html
    assert branding.progress_ring(-5, "x").count("--p:0") == 1  # clamped to 0


def test_brand_css_is_a_style_block():
    assert branding.BRAND_CSS.strip().startswith("<style>")
    assert "p-good" in branding.BRAND_CSS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_branding.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.branding'`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/branding.py
"""Presentation helpers: the theme CSS and small HTML builders (SPEC: UI-VISUAL-DIRECTION §5).

Kept pure and Streamlit-free so they unit-test; streamlit_app injects/renders the returned strings.
"""
import html as _html

from app.labels import status_label

# code -> semantic pill class
_PILL_CLASS = {"open": "p-warn", "manager_responded": "p-good", "closed": "p-done"}

BRAND_CSS = """<style>
:root{
  --blue:#0063F2; --blue-fill:#1F74FF; --yellow:#FFDE17; --sky:#24A9FF; --toki:#FFB414;
  --surface2:#322F2B; --surface3:#3A3733; --border:#454039; --ink:#F6F5F2; --muted:#A49F97;
  --good:#37C77E; --warn:#FFB414; --bad:#FF5B52; --done:#8B8781;
}
.brand-stripe{height:3px;border-radius:2px;margin:0 0 10px;
  background:linear-gradient(90deg,var(--blue),var(--sky) 55%,var(--yellow));}
.pill{display:inline-flex;align-items:center;gap:7px;font-size:.82rem;font-weight:600;
  padding:4px 12px;border-radius:999px;}
.pill::before{content:"";width:8px;height:8px;border-radius:50%;background:currentColor;}
.p-good{color:var(--good);background:rgba(55,199,126,.14);}
.p-warn{color:var(--warn);background:rgba(255,180,20,.15);}
.p-bad{color:var(--bad);background:rgba(255,91,82,.14);}
.p-done{color:var(--done);background:rgba(139,135,129,.16);}
.p-info{color:var(--sky);background:rgba(36,169,255,.14);}
.stat-tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;}
.tile{border:1px solid var(--border);border-radius:12px;padding:15px 16px;background:var(--surface2);
  position:relative;overflow:hidden;}
.tile::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--stripe,var(--blue));}
.tile .k{font-size:.74rem;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:700;}
.tile .v{font-size:2rem;font-weight:800;letter-spacing:-.02em;font-variant-numeric:tabular-nums;margin-top:4px;color:var(--ink);}
.ring{width:112px;height:112px;border-radius:50%;display:grid;place-items:center;
  background:conic-gradient(var(--good) calc(var(--p,0)*1%),var(--surface3) 0);}
.ring .inner{width:84px;height:84px;border-radius:50%;background:var(--surface2);display:grid;place-items:center;text-align:center;}
.ring .pct{font-size:1.35rem;font-weight:800;color:var(--ink);font-variant-numeric:tabular-nums;}
.ring .cap{font-size:.62rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:700;}
</style>"""


def brand_stripe() -> str:
    return '<div class="brand-stripe"></div>'


def status_pill(code: str, suffix=None) -> str:
    label = status_label(code)
    if suffix:
        label = f"{label} · {suffix}"  # "label · suffix"
    cls = _PILL_CLASS.get(code, "p-done")
    return f'<span class="pill {cls}">{_html.escape(label)}</span>'


def progress_ring(pct: int, caption: str) -> str:
    p = max(0, min(100, int(pct)))
    return (f'<div class="ring" style="--p:{p}"><div class="inner"><div>'
            f'<div class="pct">{p}%</div><div class="cap">{_html.escape(caption)}</div>'
            f'</div></div></div>')
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_branding.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add app/branding.py tests/test_branding.py
git commit -m "feat: add app/branding CSS and HTML component helpers"
```

---

### Task 3: `.streamlit/config.toml` — dark 51 Talk theme

**Files:**
- Create: `.streamlit/config.toml`

**Interfaces:** none (Streamlit reads this file at startup).

> Note: `.streamlit/secrets.toml` is gitignored, but `config.toml` is **not** a secret and SHOULD be committed. Confirm `.gitignore` ignores only `secrets.toml` (it does — line 36 `/.streamlit/secrets.toml`), so `config.toml` will commit normally.

- [ ] **Step 1: Create the theme file**

```toml
# .streamlit/config.toml — 51 Talk dark brand theme (SPEC: UI-VISUAL-DIRECTION §3, §8)
[theme]
base = "dark"
primaryColor = "#0063F2"
backgroundColor = "#201F1D"
secondaryBackgroundColor = "#2A2825"
textColor = "#F6F5F2"
font = "sans serif"
```

- [ ] **Step 2: Verify the app boots with the theme**

Run: `python -m streamlit run streamlit_app.py` (Ctrl+C after it loads).
Expected: app starts; background is warm-gray dark, primary buttons are blue. (No DB actions needed to see the theme.)

- [ ] **Step 3: Commit**

```bash
git add .streamlit/config.toml
git commit -m "feat: add dark 51 Talk Streamlit theme"
```

---

### Task 4: Wire global branding into `streamlit_app.py`

Import the new modules, replace the inline `VERDICTS`/`VERDICT_LABEL` with the ones from `labels`, inject the CSS + brand stripe once, and add the logo.

**Files:**
- Modify: `streamlit_app.py` (imports ~15-24; the `VERDICTS`/`VERDICT_LABEL` block ~34-45; `st.set_page_config` ~26)

**Interfaces:**
- Consumes: `app.labels`, `app.branding`

- [ ] **Step 1: Add imports**

After the existing `from app import data` line, add:

```python
from app import branding, labels
```

- [ ] **Step 2: Replace the inline verdict maps with the shared ones**

Delete the inline block (the `VERDICTS = {…}` dict through `VERDICT_LABEL["leave"] = "On Leave"`) and replace with:

```python
# Verdict maps now live in app/labels (shared by TL page, HRBP override, and export).
VERDICTS = labels.VERDICTS
VERDICT_LABEL = labels.VERDICT_LABEL
```

- [ ] **Step 3: Inject theme CSS + logo once, right after `st.set_page_config(...)`**

```python
st.markdown(branding.BRAND_CSS, unsafe_allow_html=True)
# Logo slot — swap the path for the real 51 Talk asset when available.
# st.logo("assets/51talk-logo.png")   # uncomment once the asset is committed
```

- [ ] **Step 4: Verify nothing broke**

Run: `python -m pytest -q`
Expected: all existing tests + Tasks 1-2 tests pass (no test imports `streamlit_app`, so this confirms `labels`/`branding` are import-clean).

Run: `python -m streamlit run streamlit_app.py` (Ctrl+C after load).
Expected: app loads, no ImportError, styling present.

- [ ] **Step 5: Commit**

```bash
git add streamlit_app.py
git commit -m "refactor: source verdict maps from labels, inject brand CSS"
```

---

### Task 5: HRBP dashboard — plain language + branded tiles & ring

**Files:**
- Modify: `streamlit_app.py` — the `st.tabs([...])` list (~242-244); the Dashboard `tab_dash` block (metrics ~248-255); the finalized-override caption uses codes indirectly (leave as-is).

**Interfaces:**
- Consumes: `labels.status_label`, `branding.brand_stripe`, `branding.progress_ring`, `branding.status_pill`

- [ ] **Step 1: Relabel the tab names (display only)**

In the `st.tabs([...])` call, change the two jargon labels; keep the rest:

```python
tab_dash, tab_ingest, tab_exc, tab_links, tab_close, tab_export, tab_uploads = st.tabs(
    ["📋 Overview", "⬆️ Import data", "⚠️ Exceptions", "🔗 Team-leader links",
     "🔒 Close period", "📤 Export", "🗂️ Uploads"])
```

- [ ] **Step 2: Replace the three metrics with branded tiles + a cycle ring**

Inside `with tab_dash:`, replace the `counts = …` + `m = st.columns(3)` + three `m[i].metric(...)` lines with:

```python
counts = data.counts_by_status(c)
awaiting = counts.get("open", 0)
confirmed = counts.get("manager_responded", 0)
finalized = counts.get("closed", 0)
total = awaiting + confirmed + finalized
done_pct = round(100 * (confirmed + finalized) / total) if total else 0

left, right = st.columns([3, 1])
with left:
    st.markdown(
        '<div class="stat-tiles">'
        f'<div class="tile" style="--stripe:var(--toki)"><div class="k">Awaiting TLs</div><div class="v">{awaiting}</div></div>'
        f'<div class="tile" style="--stripe:var(--good)"><div class="k">Confirmed</div><div class="v">{confirmed}</div></div>'
        f'<div class="tile" style="--stripe:var(--done)"><div class="k">Finalized</div><div class="v">{finalized}</div></div>'
        '</div>', unsafe_allow_html=True)
with right:
    st.markdown(branding.progress_ring(done_pct, f"{confirmed + finalized} / {total} cases"),
                unsafe_allow_html=True)
```

- [ ] **Step 3: Human-word the status filter (display only, codes preserved)**

Replace the `status_filter = st.selectbox("Show", [...])` + `rows = data.list_cases(...)` lines with:

```python
FILTER_MAP = {"Confirmed": "manager_responded", "Awaiting confirmation": "open",
              "Finalized": "closed", "All statuses": None}
choice = st.selectbox("Show", list(FILTER_MAP))
rows = data.list_cases(c, status=FILTER_MAP[choice])
st.dataframe(rows, use_container_width=True, hide_index=True)
```

- [ ] **Step 4: Add the brand stripe at the top of the dashboard tab**

As the first line inside `with tab_dash:`, add:

```python
st.markdown(branding.brand_stripe(), unsafe_allow_html=True)
```

- [ ] **Step 5: Verify**

Run: `python -m pytest -q` → all pass.
Run the app, log in as HRBP, open **Overview**: tiles show striped counts, the ring shows cycle %, the filter reads human words and still filters correctly (codes unchanged in the DB). Confirm the reconciled **Export** still works (regression: `VERDICT_LABEL` unchanged).

- [ ] **Step 6: Commit**

```bash
git add streamlit_app.py
git commit -m "feat: brand the HRBP dashboard and use plain-language statuses"
```

---

### Task 6: TL page — "Select…" default, submit guard, plain language, progress

This is the **one behaviour change**: a TL must now actively choose each verdict.

**Files:**
- Modify: `streamlit_app.py` — `render_tl` (the pending-form block ~159-197; the `source_status` copy ~166-167 and ~208-209)

**Interfaces:**
- Consumes: `labels.SELECT_PLACEHOLDER`, `labels.missing_verdicts`, `branding.brand_stripe`

- [ ] **Step 1: Add the brand stripe + progress cue**

At the top of `render_tl`, right after `st.title("Attendance Verification")`, add:

```python
st.markdown(branding.brand_stripe(), unsafe_allow_html=True)
```

After `pending`/`done` are computed and before the `with st.form("verify"):`, add a progress line:

```python
if pending or done:
    total_ct = len(pending) + len(done)
    st.caption(f"**{len(done)} of {total_ct}** confirmed so far.")
```

- [ ] **Step 2: Make the verdict dropdown default to "Select…"**

In the pending loop, change the verdict selectbox and how the choice is stored. Replace:

```python
verdict = col1.selectbox("Verdict", list(VERDICTS.keys()), key=f"v{cs['id']}")
comment = col2.text_input("Comment (evidence, e.g. approved leave email)", key=f"c{cs['id']}")
upl = col2.file_uploader("Attach proof (pdf/jpg/png)", type=["pdf", "jpg", "jpeg", "png"], key=f"f{cs['id']}")
choices[cs["id"]] = (VERDICTS[verdict], None, comment, upl)
```

with:

```python
verdict = col1.selectbox("Your verdict", [labels.SELECT_PLACEHOLDER, *VERDICTS.keys()],
                         key=f"v{cs['id']}")
comment = col2.text_input("Comment (evidence, e.g. approved leave email)", key=f"c{cs['id']}")
upl = col2.file_uploader("Attach proof (pdf/jpg/png)", type=["pdf", "jpg", "jpeg", "png"], key=f"f{cs['id']}")
choices[cs["id"]] = (verdict, None, comment, upl)   # store the LABEL; map to code after the guard
```

- [ ] **Step 3: Also make the "flagged as" copy plainer**

Change the pending-row markdown `flagged as *{cs['source_status']}*{hd}` to `system flagged as *{cs['source_status']}*{hd}`. Do the same in the `done` loop line (`flagged *…*` → `system flagged as *…*`).

- [ ] **Step 4: Guard the submit, then map labels → codes**

Replace the submit handler. The old loop unpacked `(ms, lt, cm, upl)` where `ms` was already a code; now the first element is a **label**, so guard first, then map. Replace:

```python
if st.form_submit_button("Submit all", type="primary"):
    actor = f"tl:{mgr['crm']}"
    sc = storage_client()
    ok = stale = files = 0
    for cid, (ms, lt, cm, upl) in choices.items():
        if not data.submit_verdict(c, cid, ms, lt, cm, actor):
```

with:

```python
if st.form_submit_button("Confirm all", type="primary"):
    unfilled = labels.missing_verdicts({cid: v[0] for cid, v in choices.items()})
    if unfilled:
        st.error(f"Please choose a verdict for every day — {len(unfilled)} still say “Select…”.")
        st.stop()
    actor = f"tl:{mgr['crm']}"
    sc = storage_client()
    ok = stale = files = 0
    for cid, (vlabel, lt, cm, upl) in choices.items():
        ms = VERDICTS[vlabel]  # label -> stored code, now that all are chosen
        if not data.submit_verdict(c, cid, ms, lt, cm, actor):
```

(The rest of the loop body — `stale += 1`, attachment upload, `st.session_state["tl_thanks"]`, `st.rerun()` — is unchanged.)

- [ ] **Step 5: Verify the behaviour change**

Run: `python -m pytest -q` → all pass (the guard logic is covered by `test_labels.missing_verdicts`).
Run the app, open a TL link with open cases:
- The verdict dropdown shows **"Select…"** first and does not pre-pick Present.
- Submitting with any day unchosen shows the error and saves nothing.
- Choosing every verdict then submitting records them (one-submit lock still holds — `submit_verdict` unchanged).

- [ ] **Step 6: Commit**

```bash
git add streamlit_app.py
git commit -m "feat: TL page requires an explicit verdict (Select… default) + plain copy"
```

---

## Self-Review

**Spec coverage:**
- §3 tokens → Task 3 (theme) + Task 2 (`BRAND_CSS`). ✓
- §4 typography → `font="sans serif"` (Task 3); system stack is Streamlit's default sans. ✓
- §5 identity/stripe/logo → Task 4 (CSS, `st.logo`, stripe helper), used in Tasks 5-6. ✓
- §5 TL page → Task 6. ✓
- §5 HRBP tiles/ring/pills → Task 5. ✓ (dataframe shows human words via filter; colored pills live in the tiles/ring per the Streamlit-dataframe constraint noted below.)
- §6 behaviour change → Task 6. ✓
- §7 vocabulary → Task 1 (`STATUS_LABELS`) applied in Tasks 5-6; tab renames in Task 5. ✓
- §8 seams → Tasks 3-6 map 1:1. ✓

**Known constraint (documented, not a gap):** `st.dataframe` cannot render HTML, so the worklist table shows plain human-word status values, not colored pills. `status_pill` is built and unit-tested for the tiles and any future custom HTML table; a fully custom pill-table worklist is intentionally deferred (see spec §9 "not now").

**Placeholder scan:** none — every code step shows complete code. ✓

**Type consistency:** `choices[id]` first element changed from *code* to *label* in Task 6, and every consumer (`missing_verdicts` input, the `VERDICTS[vlabel]` map, the unpack `(vlabel, lt, cm, upl)`) is updated together. `VERDICTS`/`VERDICT_LABEL` names preserved so `report.py` and the HRBP override are untouched. ✓

**Regression watch:** `VERDICT_LABEL` is unchanged in value, so the reconciled export (Task 5 verify step) and the finalized-override still work.
