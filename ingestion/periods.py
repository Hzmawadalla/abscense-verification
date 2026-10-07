"""Attendance Period: the business period is always the 15th of one month through the 14th of the
next (e.g. 2026-09-15 → 2026-10-14). It is global — not per team — and it is derived from each
day's work_date, never from an upload's file name or range: one workbook can span two periods.

This is the single Python definition. The SQL functions attendance.period_start/period_end
(Phase 1 migration) implement the same rule; tests/pg asserts the two agree day by day.
"""
import datetime

PERIOD_START_DAY = 15


def _add_months(d: datetime.date, months: int) -> datetime.date:
    """d (a day <= 28) shifted by whole months, rolling the year."""
    m = d.month - 1 + months
    return d.replace(year=d.year + m // 12, month=m % 12 + 1)


def attendance_period(work_date: datetime.date) -> tuple[datetime.date, datetime.date]:
    """work_date -> (period_start, period_end). The 15th opens a period; the 14th closes one."""
    if isinstance(work_date, datetime.datetime):
        work_date = work_date.date()
    anchor = work_date.replace(day=PERIOD_START_DAY)
    start = anchor if work_date.day >= PERIOD_START_DAY else _add_months(anchor, -1)
    end = _add_months(start, 1) - datetime.timedelta(days=1)
    return start, end


def period_dates(period_start: datetime.date) -> list[datetime.date]:
    """Every date of the period that begins on period_start (which must be a 15th)."""
    if period_start.day != PERIOD_START_DAY:
        raise ValueError(f"an attendance period starts on the 15th, got {period_start}")
    _, end = attendance_period(period_start)
    return [period_start + datetime.timedelta(days=i)
            for i in range((end - period_start).days + 1)]
