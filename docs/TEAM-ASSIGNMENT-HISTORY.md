# Team-assignment history (Phase 3, PR B)

**Status: PROPOSAL for the migration and deploy. Nothing applied to Production.**

## The rule
A TL's team view follows what the reference (HC + Structure) uploads showed. It uses the
**upload dates** (Africa/Cairo), which are a conservative stand-in for the real HR move date.

- **Previous TL** keeps the days up to the last upload that showed them as TL.
- **New TL** gets the days from the first upload that shows the move.
- **Days in between** belong to nobody's team view.
- **Before the first recorded move,** the current TL sees the employee's whole history, as today.
- **A stretch with no TL** (unmapped in Structure) is nobody's team.
- **Same-day changes:** if two uploads on the same day show different TLs, both TLs see that day.
- **Cases are separate.** A TL always sees days with a case assigned to them.
- **HRBP views are unchanged.**

`app/team_scope.py` has the exact rules. `tests/test_team_scope.py` and
`tests/pg/test_team_assignments.py` prove them.

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
- **Gaps are permanent.** History is append-only. A wrong Structure upload (e.g. a TL left
  unmapped once) leaves a permanent gap of nobody's days. Closing it needs a separately approved
  repair.
- **Rollback.** The migration is forward-only. `drop table attendance.team_assignments` is
  possible only before the PR B code is deployed, and it loses every recorded move.
