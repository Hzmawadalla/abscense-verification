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


def test_a_first_no_tl_stretch_gives_the_later_tl_no_earlier_days():
    s = [Stretch(None, D(2026, 9, 1), D(2026, 9, 1)), Stretch(A, D(2026, 9, 20), D(2026, 9, 20))]
    assert not on_team(s, A, A, D(2026, 9, 10)) and on_team(s, A, A, D(2026, 9, 20))
