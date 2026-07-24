# UI Visual Direction — Attendance Verification

> Status: **Design direction, approved 2026-07-24.** No application code has been changed.
> This document is the contract for a later implementation session. Interactive mockup:
> https://claude.ai/code/artifact/d1171da6-effb-4a1b-85c4-4e2ad2e4292c

## 1. Goal & scope

A **whole-app "feel" pass** so both surfaces read as one intentional 51 Talk HR product. Three
approved outcomes:

1. **Professional polish** — a chosen palette, typography, and spacing instead of default Streamlit gray.
2. **Trust / official look** — a named, branded identity so a Team Leader receiving a bare link trusts it.
3. **Clearer language** — one human vocabulary replacing internal status codes everywhere.

**Out of scope (deliberately):** bilingual / Arabic-RTL support, structural reorganisation of the HRBP
tabs, and any change to business logic beyond the one flagged in §6.

## 2. Constraints (the Streamlit canvas)

The "feel" levers are a fixed set, and every decision below maps onto them:

- **`.streamlit/config.toml` `[theme]`** — `base="dark"`, `primaryColor`, background/surface/text colours,
  font family. Propagates to all 7 tabs + the TL page at once.
- **`st.logo(...)`** — the header logo (swappable slot for the real asset).
- **Scoped CSS via `st.markdown(..., unsafe_allow_html=True)`** — for the brand stripe, status pills,
  attention stripes, stat-tile stripes, and the progress ring (things the theme can't express).
- **Copy / labels** — the wording swaps in §5 are plain Python string changes.

**Committed to dark** per the client — this is a single-world design, not a light/dark toggle.

## 3. Design tokens

Warm-gray ground (a gray scale biased toward Font Gray's hue, so neutrals read as *chosen*), the official
51 Talk kit with **one role per colour**, and semantic green/red added only where meaning demands it.

### Neutrals (warm-gray dark)
| Token | Hex | Use |
|---|---|---|
| `ground` | `#201F1D` | page base |
| `surface` | `#2A2825` | cards, elevated |
| `surface-2` | `#322F2B` | tiles, inner panels |
| `surface-3` | `#3A3733` | inputs, locked rows |
| `border` | `#454039` | hairlines |
| `ink` | `#F6F5F2` | primary text |
| `ink-2` | `#D9D5CF` | body text |
| `muted` | `#A49F97` | secondary/labels |

### 51 Talk brand kit — one role each
| Colour | Hex | Role (never crosses into another) |
|---|---|---|
| **51Talk Blue** | `#0063F2` | Primary actions, links, active tab, focus ring |
| **51Talk Yellow** | `#FFDE17` | Brand signature ONLY — brand stripe, logo, section accents. Used sparingly; never a button or status. |
| **Sky Blue** | `#24A9FF` | Info, "attach proof", "on leave" |
| **Toki Yellow** | `#FFB414` | "Awaiting" attention — left stripe on cards/rows |
| **Font Gray** | `#3E3A39` | Surface/border tone (folded into the neutral scale) |

> Interactive contrast note: white text on true `#0063F2` is borderline on dark, so **button fills use a
> lifted `#1F74FF`** while `#0063F2` stays the identity/swatch hex. "Brand hex for identity, tuned hex for
> contrast."

### Semantic (added, tuned for the dark ground)
| Meaning | Hex | Where |
|---|---|---|
| Confirmed (good) | `#37C77E` | Confirmed pills/tiles, locked-result text |
| Awaiting (warn) | `#FFB414` (Toki) | Awaiting pills, attention stripe |
| Absent (danger) | `#FF5B52` | Absent pills |
| Finalized (neutral-done) | `#8B8781` | Finalized pills/tiles |
| On leave (info) | `#24A9FF` (Sky) | On-leave pills |

Each pill = coloured text on a ~14% tint of the same hue.

## 4. Typography

Clean system sans (`"Segoe UI", system-ui, -apple-system, …`) — matches the Windows/Streamlit reality with
no risky webfont. Differentiate by weight/size/spacing, not by family. Monospace (`Cascadia Code`/`ui-mono`)
for CRM IDs and links. **Tabular numerals** for metric tiles and the progress ring.

## 5. Component decisions

### Identity (both surfaces)
- Thin **brand stripe** (Blue → Sky → Yellow) across the top of every screen.
- Named header: **"Attendance Verification"** + context line ("People Operations · 51 Talk" for TLs;
  "HRBP workspace" for HR).
- **Logo slot** = one token; the real 51 Talk logo drops in via `st.logo`.
- TL header carries a **"🔒 Secure personal link"** trust cue.

### TL page (`render_tl`)
- Progress cue ("1 of 3 confirmed", Blue→Sky fill).
- **One card per day**; a **Toki left-stripe** on days still needing a verdict.
- Plain labels: **"Your verdict"**, **"system flagged as …"**, **"Already submitted · locked"**.
- Locked results show the final verdict in green.
- Sticky primary **"Confirm all — submit N verdicts"** button (Blue).

### HRBP dashboard (Overview tab)
- Three **stat tiles** with a colour stripe each (Awaiting=Toki, Confirmed=green, Finalized=gray),
  relabelled per §5 vocabulary.
- A **cycle-progress ring** ("67% · 12/18 teams responded") — the "where does the cycle stand" answer.
- Worklist table where each row's state reads via a coloured **status pill**.

## 6. The one behaviour change (flag for review)

Everything above is presentation. **The single functional change** is the TL verdict default: today the
dropdown pre-selects **"Present"** (first key in `VERDICTS`), so a TL can submit without actively choosing.
The direction replaces this with a **"Select…" placeholder** requiring an explicit choice, closing the
"everything silently defaults to Present" data-quality risk.

This needs a small change in `render_tl` (a sentinel option + a "please choose a verdict for every day"
guard before submit). **Call this out explicitly at implementation time** — it changes what TLs must do.

## 7. Plain-language vocabulary map

| Users currently see | Proposed word | Where |
|---|---|---|
| `open` | **Awaiting confirmation** | TL page, dashboard filter & pill |
| `manager_responded` | **Confirmed** | dashboard tile, filter, pill |
| `closed` | **Finalized** | dashboard, export |
| `source_status` (raw) | **System flagged as …** | every case row |
| `(all)` | **All statuses** | filter dropdown |
| `sent` / `failed` | **Sent** / **Send failed** | links tab |
| `Ingest` | **Import data** | tab name |
| `TL links` | **Team-leader links** | tab name |

These are **display-only** relabels — stored enum codes (`open`, `manager_responded`, `closed`, verdict
codes) are unchanged, so the DB and exports are untouched.

## 8. Implementation seams (for the later session)

1. `.streamlit/config.toml` — `base="dark"`, `primaryColor="#0063F2"`, background `#201F1D`,
   secondaryBackground `#2A2825`, text `#F6F5F2`, `font="sans serif"`.
2. A single `st.markdown` CSS block (one place) defining the brand stripe, `.pill-*`, tile stripes,
   attention stripe, and the ring — driven by the tokens in §3 so a colour change is one edit.
3. `st.logo` with the real asset (sized for the ~38px header slot).
4. A shared display-label map for statuses (§7), used by both `render_tl` and `render_hrbp`.
5. The §6 "Select…" default + submit guard in `render_tl`.

All five are additive and low-risk; none touch ingestion, the DB schema, or dispatch.

## 9. Future / not now

- Real 51 Talk logo asset (client can provide) → drop into the slot.
- Bilingual (Arabic/EN, RTL) — separate effort.
- Structural tab reorganisation (cycle-phase grouping) — separate effort.
