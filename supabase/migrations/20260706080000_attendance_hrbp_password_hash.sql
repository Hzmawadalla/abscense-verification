-- Per-user HRBP login (Streamlit, bcrypt via streamlit-authenticator).
set search_path = attendance, public;

alter table attendance.hrbp_users
  add column if not exists password_hash text,
  add column if not exists name text;
