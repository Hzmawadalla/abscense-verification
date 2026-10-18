"""Warnings for suspicious team changes in a reference upload (Phase 3, decision F).

ADVISORY ONLY: nothing here changes, blocks or rejects an upload. It is read before HRBP confirms.

A reference workbook carries no reliable date of its own, so an OLDER HC/Structure file uploaded
later cannot be recognised with certainty. Every move it shows is recorded with the upload date
(app.team_scope), so what can be checked is its effect on the recorded history:

  * REVERSE move — an employee would move to a TL they were already recorded under before, but
    who is not their TL today. This is the typical sign of an older file. Loading it records the
    move with today's date: that earlier TL gets the employee's days from today, and the current
    TL's days stop at the last upload that showed them.
  * BROAD change — at least BROAD_MIN employees, and at least BROAD_SHARE of the listed employees
    who already exist, would change TL in this one upload. Real reorganisations do this too, so it
    is a prompt to check the file, not a verdict.
  * Move to NO TL — an employee with a TL today would have none. Their days from today belong to
    nobody's team view until a later upload maps them (the gap is permanent: history is never
    rewritten).

Comparisons use the exact CRM strings the loader matches on (employees.crm, managers.crm).
"""
from dataclasses import dataclass

BROAD_MIN = 10          # a heuristic, not a business rule: see the module docstring
BROAD_SHARE = 0.10
SAMPLE = 5              # how many example CRMs a warning names


@dataclass(frozen=True)
class AssignmentChanges:
    listed_existing: int            # employees in the file who already exist in the database
    moves: tuple                    # CRMs whose TL would change (including to or from no TL)
    reverse: tuple                  # moves back to a TL already recorded for them, not current
    to_none: tuple                  # CRMs with a TL today who would have none

    @property
    def broad(self) -> bool:
        return (len(self.moves) >= BROAD_MIN
                and len(self.moves) >= BROAD_SHARE * max(self.listed_existing, 1))


def classify(incoming, current, history) -> AssignmentChanges:
    """Pure. `incoming` {employee_crm: tl_crm or None} from the file; `current` {employee_crm:
    tl_crm or None} for the employees that already exist; `history` {employee_crm: [tl_crm or
    None, ...]} recorded stretches in order (missing = none recorded)."""
    moves, reverse, to_none = [], [], []
    for crm, new_tl in sorted(incoming.items()):
        if crm not in current:
            continue                                        # a new employee: nothing to compare
        now = current[crm]
        if new_tl == now:
            continue
        moves.append(crm)
        if new_tl is None:
            to_none.append(crm)
        elif new_tl in history.get(crm, ()):
            reverse.append(crm)
    listed = sum(1 for crm in incoming if crm in current)
    return AssignmentChanges(listed, tuple(moves), tuple(reverse), tuple(to_none))


_CURRENT_SQL = """
select e.crm, m.crm as tl from attendance.employees e
  left join attendance.managers m on m.id = e.manager_id
 where e.crm = any(%s)
"""
_HISTORY_SQL = """
select e.crm, m.crm as tl from attendance.team_assignments t
  join attendance.employees e on e.id = t.employee_id
  left join attendance.managers m on m.id = t.manager_id
 where e.crm = any(%s)
 order by e.crm, t.first_seen_on, t.id
"""


def read_changes(conn, ref) -> AssignmentChanges:
    """Read-only: what loading `ref` would change in team assignments."""
    incoming = {e.crm: e.manager_crm for e in ref.employees}
    crms = list(incoming)
    if not crms:
        return AssignmentChanges(0, (), (), ())
    with conn.cursor() as cur:
        cur.execute(_CURRENT_SQL, (crms,))
        current = dict(cur.fetchall())
        cur.execute(_HISTORY_SQL, (crms,))
        history = {}
        for crm, tl in cur.fetchall():
            history.setdefault(crm, []).append(tl)
    return classify(incoming, current, history)


UNAVAILABLE = "unavailable"      # read_changes_or_none: the check could not run


def read_changes_or_none(conn, ref):
    """read_changes for the preview. Advisory only, so it can never block a preview: on any error
    it returns UNAVAILABLE (warning_lines says so) and leaves the connection usable."""
    try:
        return read_changes(conn, ref)
    except Exception:                                        # noqa: BLE001 — never block a preview
        try:
            conn.rollback()                                  # a no-op on an autocommit connection
        except Exception:                                    # noqa: BLE001
            pass
        return UNAVAILABLE


def _examples(crms) -> str:
    shown = ", ".join(crms[:SAMPLE])
    return shown + (f" and {len(crms) - SAMPLE} more" if len(crms) > SAMPLE else "")


def warning_lines(changes, stats) -> list[str]:
    """Preview warnings (markdown) for the HRBP page; empty when nothing looks suspicious."""
    lines = []
    if changes == UNAVAILABLE:
        lines.append("⚠️ The team-change check could not run (the database read failed), so no "
                     "warning about TL moves is shown for this upload. Check the HC/Structure file "
                     "is the latest one before loading.")
        changes = None
    if changes is not None and changes.reverse:
        lines.append(f"⚠️ {len(changes.reverse)} employee(s) would move **back** to a TL they were "
                     f"recorded under before ({_examples(changes.reverse)}). This often means the "
                     "HC/Structure file is **older** than the last one loaded. Loading it records "
                     "these moves with today's date: the earlier TL sees their days from today, and "
                     "the current TL's days stop at the last upload that showed them.")
    if changes is not None and changes.broad:
        share = round(100 * len(changes.moves) / max(changes.listed_existing, 1))
        lines.append(f"⚠️ {len(changes.moves)} of {changes.listed_existing} existing employees "
                     f"({share}%) would change TL in this one upload ({_examples(changes.moves)}). "
                     "Check this is the latest HC/Structure file: every move is recorded with "
                     "today's date.")
    if changes is not None and changes.to_none:
        lines.append(f"⚠️ {len(changes.to_none)} employee(s) who have a TL today would have **no "
                     f"TL** ({_examples(changes.to_none)}). From today their days belong to no "
                     "TL's team view until a later upload maps them.")
    conflicting = list((stats or {}).get("conflicting_tl_crms") or [])
    if conflicting:
        lines.append(f"⚠️ {len(conflicting)} CRM(s) are listed more than once with **different "
                     f"TLs** ({_examples(conflicting)}) — {stats.get('conflict_rule')}. Fix the "
                     "file if that is not the right TL.")
    dups = list((stats or {}).get("duplicate_crms") or [])
    if dups:
        lines.append(f"⚠️ {len(dups)} CRM(s) are listed more than once in the employee list "
                     f"({_examples(dups)}) — {stats.get('duplicate_rule')}.")
    return lines
