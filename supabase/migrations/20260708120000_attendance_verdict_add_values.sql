-- Extend the verdict enum with explicit leave types + half day (design 2026-07-08).
-- 'present'/'absent' retained; legacy 'leave' kept but unused (Postgres cannot cleanly drop an
-- enum value). No data migration — manager_status/final_status are empty at time of change.
set search_path = attendance, public;

alter type verdict add value if not exists 'annual_leave';
alter type verdict add value if not exists 'unpaid_leave';
alter type verdict add value if not exists 'sick_leave';
alter type verdict add value if not exists 'half_day';
