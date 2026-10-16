"""Which days of an employee a TL's team view may show (Phase 3 PR B). Pure — no database.

Source: attendance.team_assignments, one stretch per employee x TL as observed by reference
(HC + Structure) uploads. first_seen_on / last_seen_on are OBSERVATION dates (the upload days):
a conservative proxy for the real HR move date, never the move date itself.

Rules (approved 2026-10-16), for the stretches of one employee in order:
  * the FIRST stretch also covers every earlier day ("before the first recorded move", so a TL
    keeps the history of an employee who never moved — the behaviour before PR B);
  * the LAST stretch also covers every later day, but only while it still matches the employee's
    current TL (employees.manager_id); otherwise it ends at its last_seen_on (fail closed);
  * any other stretch covers first_seen_on .. last_seen_on, both inclusive;
  * days between two stretches (between the last upload showing the old TL and the first showing
    the new one) belong to NOBODY's team view: the move happened somewhere in there;
  * a stretch with no TL (manager_id None) is nobody's team;
  * an employee with no stretch at all (never seen since PR B) keeps the pre-PR B rule: their
    current TL, every day.
So the previous TL keeps the days up to the last upload that showed them as TL, the new TL gets
the days from the first upload that shows the move, and neither sees the other's stretch.
Case days are separate: a TL always sees days with a case assigned to them (app.attendance_view).
"""
import datetime as dt
from typing import NamedTuple


class Stretch(NamedTuple):
    manager_id: object
    first_seen_on: dt.date
    last_seen_on: dt.date


def on_team(stretches, current_manager_id, manager_id, day) -> bool:
    """Whether `day` of this employee is on `manager_id`'s team. `stretches` in recorded order."""
    if manager_id is None:
        return False
    if not stretches:
        return current_manager_id == manager_id
    last = len(stretches) - 1
    for i, s in enumerate(stretches):
        if s.manager_id != manager_id:
            continue
        starts = i == 0 or day >= s.first_seen_on
        open_end = i == last and s.manager_id == current_manager_id
        ends = open_end or day <= s.last_seen_on
        if starts and ends:
            return True
    return False
