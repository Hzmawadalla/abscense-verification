"""app.team_scope: which days of an employee a TL's team view may show (Phase 3 PR B)."""
import datetime as dt

from app.team_scope import Stretch, on_team

D = dt.date
A, B, C = "tl-a", "tl-b", "tl-c"


def days(stretches, current, tl, first=D(2026, 8, 1), last=D(2026, 12, 31)):
    n = (last - first).days + 1
    return {first + dt.timedelta(i) for i in range(n)
            if on_team(stretches, current, tl, first + dt.timedelta(i))}


def test_no_history_keeps_the_pre_history_rule_current_tl_every_day():
    assert on_team([], A, A, D(2020, 1, 1)) and on_team([], A, A, D(2030, 1, 1))
    assert not on_team([], A, B, D(2026, 9, 20))
    assert not on_team([], None, None, D(2026, 9, 20))


def test_one_stretch_covers_all_history_and_the_future():
    s = [Stretch(A, D(2026, 10, 16), D(2026, 10, 16))]
    assert on_team(s, A, A, D(2025, 1, 1)) and on_team(s, A, A, D(2027, 1, 1))
    assert not on_team(s, A, B, D(2026, 10, 16))


def test_a_move_splits_the_days_and_the_gap_belongs_to_nobody():
    s = [Stretch(A, D(2026, 9, 1), D(2026, 10, 1)),       # last upload showing TL-A: 1 Oct
         Stretch(B, D(2026, 10, 16), D(2026, 11, 1))]     # first upload showing TL-B: 16 Oct
    a, b = days(s, B, A), days(s, B, B)
    assert max(a) == D(2026, 10, 1) and min(a) == D(2026, 8, 1)      # old TL: pre-move days kept
    assert min(b) == D(2026, 10, 16) and max(b) == D(2026, 12, 31)   # new TL: from the boundary
    assert not a & b
    gap = {D(2026, 10, 2) + dt.timedelta(i) for i in range(14)}
    assert not gap & (a | b)


def test_boundary_days_are_inclusive():
    s = [Stretch(A, D(2026, 9, 1), D(2026, 10, 14)), Stretch(B, D(2026, 10, 15), D(2026, 10, 15))]
    assert on_team(s, B, A, D(2026, 10, 14)) and not on_team(s, B, A, D(2026, 10, 15))
    assert on_team(s, B, B, D(2026, 10, 15)) and not on_team(s, B, B, D(2026, 10, 14))


def test_the_new_tl_never_gets_pre_move_days_even_far_back():
    s = [Stretch(A, D(2026, 10, 16), D(2026, 10, 20)), Stretch(B, D(2026, 11, 1), D(2026, 11, 1))]
    assert not on_team(s, B, B, D(2026, 1, 1)) and not on_team(s, B, B, D(2026, 10, 31))


def test_a_last_stretch_that_no_longer_matches_the_current_tl_is_closed():
    # employees.manager_id says TL-C but no stretch records it yet: fail closed, nobody's future
    s = [Stretch(A, D(2026, 9, 1), D(2026, 10, 1))]
    assert on_team(s, C, A, D(2026, 10, 1)) and not on_team(s, C, A, D(2026, 10, 2))
    assert not on_team(s, C, C, D(2026, 10, 2))


def test_an_unmapped_stretch_is_nobodys_team():
    s = [Stretch(A, D(2026, 9, 1), D(2026, 9, 30)), Stretch(None, D(2026, 10, 10), D(2026, 10, 10))]
    assert not any(on_team(s, None, tl, D(2026, 10, 12)) for tl in (A, B, None))
    assert on_team(s, None, A, D(2026, 9, 30)) and not on_team(s, None, A, D(2026, 10, 1))


def test_moving_back_gives_each_stretch_its_own_days():
    s = [Stretch(A, D(2026, 9, 1), D(2026, 9, 10)), Stretch(B, D(2026, 9, 20), D(2026, 9, 30)),
         Stretch(A, D(2026, 10, 10), D(2026, 10, 10))]
    assert on_team(s, A, A, D(2026, 9, 5)) and on_team(s, A, A, D(2026, 12, 1))
    assert not on_team(s, A, A, D(2026, 9, 25)) and on_team(s, A, B, D(2026, 9, 25))
    assert not on_team(s, A, B, D(2026, 10, 10))


def test_same_day_changes_follow_recorded_order():
    s = [Stretch(A, D(2026, 9, 1), D(2026, 10, 1)), Stretch(B, D(2026, 10, 1), D(2026, 10, 1))]
    assert on_team(s, B, A, D(2026, 10, 1)) and on_team(s, B, B, D(2026, 10, 1))
    assert not on_team(s, B, B, D(2026, 9, 30))


def test_a_move_seen_on_the_15th_starts_the_new_period_only():
    s = [Stretch(A, D(2026, 9, 15), D(2026, 10, 14)), Stretch(B, D(2026, 10, 15), D(2026, 10, 15))]
    prev = [D(2026, 9, 15) + dt.timedelta(i) for i in range(30)]          # 15 Sep -> 14 Oct
    assert all(on_team(s, B, A, d) for d in prev) and not any(on_team(s, B, B, d) for d in prev)


def test_adjacent_same_tl_stretches_are_one_no_gap_inside():
    # two racing uploads can record the move twice: B, B must not open a gap inside B's stretch
    s = [Stretch(A, D(2026, 9, 1), D(2026, 9, 10)), Stretch(B, D(2026, 9, 20), D(2026, 9, 21)),
         Stretch(B, D(2026, 9, 25), D(2026, 9, 25))]
    assert on_team(s, B, B, D(2026, 9, 23)) and on_team(s, B, B, D(2026, 12, 1))
    assert not on_team(s, B, A, D(2026, 9, 23))


def test_a_first_no_tl_stretch_now_belongs_to_the_first_recorded_tl():
    # replaces the pre-2026-10-18 expectation (the later TL got no earlier days): the approved
    # first-TL rule gives those days, the no-TL day included, to the first recorded TL
    s = [Stretch(None, D(2026, 9, 1), D(2026, 9, 1)), Stretch(A, D(2026, 9, 20), D(2026, 9, 20))]
    assert on_team(s, A, A, D(2026, 9, 10)) and on_team(s, A, A, D(2026, 9, 1))
    assert on_team(s, A, A, D(2026, 9, 20)) and not on_team(s, A, B, D(2026, 9, 10))


# ------------------------------------------------------------------ approved rule (decision B)
# The user approved: the current TL owns all historical attendance before the first recorded
# transfer — a documented limitation (earlier assignment history is unknown). These tests pin it.
def test_approved_rule_the_seeded_current_tl_owns_all_history_before_the_first_transfer():
    seed = Stretch(A, D(2026, 10, 20), D(2026, 10, 20))             # migration day
    moved = [seed, Stretch(B, D(2026, 11, 10), D(2026, 11, 10))]
    assert on_team(moved, B, A, D(2024, 1, 1))                       # far back: still A's
    assert on_team(moved, B, A, D(2026, 10, 20)) and not on_team(moved, B, A, D(2026, 10, 21))
    assert not on_team(moved, B, B, D(2026, 11, 9)) and on_team(moved, B, B, D(2026, 11, 10))


def test_approved_rule_applies_to_an_employee_first_recorded_after_the_migration():
    # not seeded (no TL at migration time), first recorded under C later: C owns earlier days too —
    # the same documented limitation, stated explicitly
    s = [Stretch(C, D(2026, 11, 3), D(2026, 11, 3))]
    assert on_team(s, C, C, D(2026, 9, 1))


# ------------------------------------------------------------------ approved first-TL rule
# Approved 2026-10-18: if an employee has no TL assignment recorded at the start of tracking, the
# first subsequently recorded TL owns all earlier attendance history; later transfers keep the
# inclusive boundaries and the unowned gap.
def test_first_tl_rule_no_tl_at_tracking_start_then_tl_a_owns_all_earlier_days():
    s = [Stretch(None, D(2026, 10, 20), D(2026, 10, 20)),          # tracking start: no TL
         Stretch(A, D(2026, 11, 3), D(2026, 11, 3))]                # later: TL-A
    for day in (D(2024, 1, 1), D(2026, 9, 1), D(2026, 10, 20), D(2026, 11, 2), D(2026, 11, 3),
                D(2027, 3, 1)):
        assert on_team(s, A, A, day), day
    assert not any(on_team(s, A, tl, D(2026, 10, 20)) for tl in (B, None))


def test_first_tl_rule_then_a_transfer_keeps_the_inclusive_boundaries_and_the_gap():
    s = [Stretch(None, D(2026, 10, 20), D(2026, 10, 20)),
         Stretch(A, D(2026, 11, 3), D(2026, 11, 10)),               # A last seen 10 Nov
         Stretch(B, D(2026, 11, 20), D(2026, 11, 20))]              # B first seen 20 Nov
    a, b = days(s, B, A), days(s, B, B)
    assert min(a) == D(2026, 8, 1) and max(a) == D(2026, 11, 10)   # all earlier days, then up to A's last
    assert min(b) == D(2026, 11, 20) and max(b) == D(2026, 12, 31)
    gap = {D(2026, 11, 11) + dt.timedelta(i) for i in range(9)}     # 11-19 Nov
    assert not gap & (a | b) and not a & b


def test_first_tl_rule_does_not_touch_a_no_tl_stretch_after_the_first_tl():
    s = [Stretch(None, D(2026, 10, 20), D(2026, 10, 20)), Stretch(A, D(2026, 11, 3), D(2026, 11, 3)),
         Stretch(None, D(2026, 11, 15), D(2026, 11, 15)), Stretch(B, D(2026, 12, 1), D(2026, 12, 1))]
    assert on_team(s, B, A, D(2026, 10, 20)) and on_team(s, B, A, D(2026, 11, 3))
    assert not any(on_team(s, B, tl, D(2026, 11, 15)) for tl in (A, B))
    assert not on_team(s, B, A, D(2026, 11, 4)) and not on_team(s, B, B, D(2026, 11, 30))


def test_first_tl_rule_only_no_tl_stretches_means_nobody():
    s = [Stretch(None, D(2026, 10, 20), D(2026, 10, 20)), Stretch(None, D(2026, 11, 3), D(2026, 11, 3))]
    assert not any(on_team(s, None, tl, D(2026, 10, 1)) for tl in (A, B, None))


def test_first_tl_rule_leaves_employees_with_no_stretch_unchanged():
    assert on_team([], A, A, D(2020, 1, 1)) and not on_team([], A, B, D(2026, 9, 20))
    assert not on_team([], None, A, D(2026, 9, 20))
