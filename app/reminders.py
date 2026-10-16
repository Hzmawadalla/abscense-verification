"""Who a TL reminder goes to, and with what count (decision 2026-10-16, option A).

Pure: works on data.managers_overview rows, sends nothing. A TL is reminded about remind_cases
only — open cases minus those a period close left open (data.CASE_PERIOD_CLOSED). Every bulk send,
the per-TL Email button, the links download and the button labels take their counts and
recipients from here, so the page never promises a reminder that won't go out.
"""


def remind_count(mgr) -> int:
    """Cases this TL is reminded about. A row without the field counts as 0 (never guess)."""
    n = mgr.get("remind_cases")
    return n if isinstance(n, int) and n > 0 else 0


def with_remind_count(mgr) -> dict:
    """A copy of the row whose open_cases is the reminder count — what senders put in the message
    and record in notifications."""
    return {**mgr, "open_cases": remind_count(mgr)}


def reminder_targets(overview) -> dict:
    """{'email': rows with an email, 'no_email': rows without one, 'dingtalk': rows with a DingTalk
    userid, 'links': every row to remind} — each row a with_remind_count copy, only TLs with at
    least one case to remind. Same rule for every list, so counts and recipients agree."""
    due = [with_remind_count(m) for m in overview if remind_count(m)]
    return {"email": [m for m in due if m.get("email")],
            "no_email": [m for m in due if not m.get("email")],
            "dingtalk": [m for m in due if m.get("dingtalk_userid")],
            "links": due}
