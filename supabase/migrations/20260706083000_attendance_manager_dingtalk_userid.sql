-- TLs are notified via DingTalk work-notification (private DM), identified by DingTalk userid.
set search_path = attendance, public;

alter table attendance.managers
  add column if not exists dingtalk_userid text;
