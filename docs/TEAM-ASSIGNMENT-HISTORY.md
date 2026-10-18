# Team-assignment history (Phase 3, PR B)

**Status: PROPOSAL for the migration and deploy. Nothing applied to Production.**

## The rule
A TL's team view follows what the reference (HC + Structure) uploads showed. It uses the
**upload dates** (Africa/Cairo), which are a conservative stand-in for the real HR move date.

- **Previous TL** keeps the days up to the last upload that showed them as TL.
- **New TL** gets the days from the first upload that shows the move.
- **Days in between** belong to nobody's team view.
- **Before the first recorded move,** the current TL sees the employee's whole history, as today.
- **A stretch with no TL** (unmapped in Structure) after the employee's first recorded TL is
  nobody's team. **No TL at the start of tracking:** the first recorded TL owns all earlier days.
- **Same-day changes:** if two uploads on the same day show different TLs, both TLs see that day.
- **Cases are separate.** A TL always sees days with a case assigned to them.
- **HRBP views are unchanged.**

`app/team_scope.py` has the exact rules. `tests/test_team_scope.py` and
`tests/pg/test_team_assignments.py` prove them.

## Approved rule for history before the first recorded transfer (decision B)
**The current TL owns all historical attendance before the first recorded transfer.** This is an
approved, documented limitation: assignment history before this feature is unknown.
- The migration seeds every employee who has a TL with that TL, seen on the migration day. The
  first stretch reaches back to every earlier day, so the seeded TL sees the employee's whole
  history until a transfer is recorded.
- The same applies to an employee **first recorded later** (e.g. no TL at migration time, then
  uploaded under TL C): C sees the earlier days too.
- An employee with **no stretch at all** keeps the pre-PR B rule (current TL, every day).
- **No TL recorded at the start of tracking (approved 2026-10-18):** if an employee's first
  recorded stretches have no TL (unmapped in Structure), **the first subsequently recorded TL owns
  all earlier attendance history** — every earlier day, the no-TL days included — until a later
  recorded transfer sets a new boundary. Later transfers keep the inclusive boundaries (previous
  TL through their last observation, new TL from their first) and days between stretches stay
  unowned. A no-TL stretch *after* the first recorded TL is still nobody's.
- **Implications:**
  - *Historical data:* the earlier days are attributed to that TL even though who actually led
    the employee then is unknown — the same documented limitation as above.
  - *Visibility:* that TL's team view shows those earlier days (TL labels only), and the employee
    appears in that TL's view for past periods. Another TL never sees them; HRBP views are
    unchanged; days with a case keep following the case's own TL.
- The TL view offers the current Attendance Period and the 5 before it, which bounds how far back
  this reaches in practice. Statuses are TL labels only (no raw text, no leave subtype, no notes).
- Tests: `test_approved_rule_*` and `test_first_tl_rule_*` in `tests/test_team_scope.py`, and
  `test_first_tl_rule_*` in `tests/pg/test_assignment_history_m1.py`.

## Suspicious-upload warnings (decision F, advisory only)
Before HRBP confirms an upload, the preview warns (it never blocks or changes what is loaded):
- **Moving back** — employees who would move to a TL they were already recorded under, who is not
  their TL today. The usual sign of an **older** HC/Structure file. If loaded anyway, the move is
  recorded with today's date: the earlier TL sees their days from today, the current TL's days stop
  at the last upload that showed them.
- **Broad change** — at least 10 employees *and* at least 10% of the listed existing employees
  would change TL in one upload (`app/assignment_check.py` `BROAD_MIN`, `BROAD_SHARE`; a heuristic).
- **Losing the TL** — employees with a TL today who would have none: their days from today belong
  to nobody until a later upload maps them.
- **Duplicates** — a CRM listed more than once in HC (the first row is used), in Structure with
  different TLs (the last row's TL is used), or in a Line-Manager export (the first row is used).
- **Limit:** the workbook has no reliable date of its own, so an older file cannot be recognised
  for certain; a file with no reverse or broad pattern raises no warning. Requiring a dated export
  (and refusing older ones) would need a separately approved change to the upload format.

## Concurrency
Two uploads at the same time serialise on the employee rows they both update: the second waits
for the first to commit, then records on top of it (same TL: one stretch; different TLs: two
same-day stretches in commit order, so both TLs see that day). Tests:
`tests/pg/test_assignment_history_m1.py`.

## Upload freeze around the release (decision C — a plan; starting it needs separate approval)
- **Owner:** the HRBP lead who runs uploads, confirmed in writing before the window.
- **Start:** before the Production backup for the migration window. No attendance or reference
  uploads, no reference imports.
- **Keep it** through the migration, the PR B merge (= deploy) and the checks below.
- **If an upload happens anyway during the freeze:** do not undo anything. Record the time and file.
  Before PR B is deployed, the move is not recorded in `team_assignments` (fails closed: the new
  TL sees nothing for that employee until the first upload under the new code). After deploy, it
  is recorded normally. Report it before resuming.
- **Resume only when all hold:** the live build stamp shows the PR B commit; the app starts and the
  HRBP and TL pages load; one reference upload under the new code shows the expected preview
  warnings (or none) and records stretches (a read-only count of `reference_upload` rows); the
  user approves resuming.

## Release order (each step needs separate approval)
1. **Back up** (pre-migration dump, as in Phase 0 and Phase 1) and run the read-only preflight in
   the migration header.
2. **Apply the migration** `20261016120000_attendance_phase3_team_assignments.sql`. It is
   additive; the code running before PR B ignores the new table. Check the post-check output.
3. **Merge PR B straight after,** which deploys it. Never deploy the code before the migration:
   every upload would fail (`team_assignments` missing) and roll back.
4. **Do one reference upload soon after the deploy.** Between the migration and the first upload
   handled by the new code, any team move is not recorded. That fails closed: the new TL sees
   nothing for that employee until the next upload records the move.

## Limits
- **Upload date, not HR date.** The boundary is the date of the upload, not the HR effective
  date. It is documented as a proxy.
- **Older files.** See the warnings above: detection is advisory and heuristic.
- **Gaps are permanent.** History is append-only. A wrong Structure upload (e.g. a TL left
  unmapped once) leaves a permanent gap of nobody's days. Closing it needs a separately approved
  repair.
- **Corrections (proposal, not yet approved):** a separately approved, reviewed forward repair
  that only adds stretch rows (never deletes or edits recorded ones), with a record of who approved
  it and why. No such tool exists yet.
- **Rollback.** The migration is forward-only. `drop table attendance.team_assignments` is
  possible only before the PR B code is deployed, and it loses every recorded move.
