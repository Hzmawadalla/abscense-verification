"""TL link policy (Phase 3, PR A): which links may be issued, and what each kind of link may open.

Two switches, changed ONLY by the separately approved cutover (a reviewed commit, not config):
  ISSUE_SIGNED_LINKS  False -> new and re-sent links stay legacy, exactly as before this change, and
                      a stored legacy link is returned unchanged. True -> every link issued is a
                      signed v1 link (app.security), and re-sending a legacy link replaces it.
  LEGACY_LINK_SUNSET  None -> legacy links keep working (case-only). A UTC datetime -> from then on
                      a legacy link reads "expired".

Whatever the switches say, a signed link that has expired is replaced by a fresh signed link when
HRBP re-sends or downloads it (approved routine), and an unexpired one is returned unchanged.

Access: a legacy link opens the case-only TL page and nothing else (SCOPE_CASE_ONLY). Only a valid,
unexpired, unrevoked signed link may open the team attendance view (SCOPE_TEAM, Phase 3 PR C).
"""
import datetime as dt

ISSUE_SIGNED_LINKS = False
LEGACY_LINK_SUNSET: dt.datetime | None = None

SCOPE_TEAM = "team"
SCOPE_CASE_ONLY = "case_only"

EXPIRED_MESSAGE = "Link expired — ask your HRBP for a new link."
INVALID_MESSAGE = "This link isn't valid — ask your HRBP for a new link."
