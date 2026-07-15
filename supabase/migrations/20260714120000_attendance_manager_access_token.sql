-- Store the TL access token ENCRYPTED (Fernet, key in app secrets — never in the DB) alongside its
-- hash, so a link can be reproduced on demand — making link generation idempotent (only an explicit
-- rotate invalidates a link) without a DB compromise yielding usable tokens.
set search_path = attendance, public;

alter table attendance.managers
  add column if not exists access_token_enc text;
