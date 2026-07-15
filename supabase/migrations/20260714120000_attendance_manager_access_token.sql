-- Store the raw TL access token alongside its hash so a link can be reproduced on demand,
-- making link generation idempotent (only an explicit rotate invalidates a link).
set search_path = attendance, public;

alter table attendance.managers
  add column if not exists access_token text;
