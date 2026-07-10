"""SMTP sender for TL verification-link emails.

Provider-agnostic: point the SMTP_* / MAIL_FROM secrets at Brevo's relay
(smtp-relay.brevo.com:587), a company relay, or any SMTP server — no code change to switch.
Bulk sends reuse one authenticated connection so 40+ emails go out in seconds, not a minute.
"""
import smtplib
import ssl
from email.message import EmailMessage

SUBJECT = "Attendance verification — please confirm your team's flagged days"


def build_message(sender, to_email, tl_name, link, open_cases) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_email
    msg["Subject"] = SUBJECT
    msg.set_content(
        f"Hi {tl_name or 'there'},\n\n"
        f"You have {open_cases} attendance case(s) awaiting your confirmation.\n\n"
        f"Open your secure link to review and confirm each flagged day:\n"
        f"{link}\n\n"
        f"This link is unique to you — please don't forward it. Each case can be submitted once.\n\n"
        f"Thank you,\nHR — Attendance Verification"
    )
    return msg


class SMTPMailer:
    def __init__(self, host, port, user, password, sender):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.sender = sender

    def connect(self) -> smtplib.SMTP:
        """Open an authenticated STARTTLS session. Use as `with mailer.connect() as smtp:` for a
        batch, passing `smtp` to send_link so every message reuses the one login."""
        s = smtplib.SMTP(self.host, self.port, timeout=30)
        s.starttls(context=ssl.create_default_context())
        s.login(self.user, self.password)
        return s

    def send_link(self, to_email, tl_name, link, open_cases, smtp=None) -> None:
        """Send one TL their unique link. Reuses `smtp` if given, else opens a one-off connection.
        Raises on failure so the caller can record the outcome per recipient."""
        msg = build_message(self.sender, to_email, tl_name, link, open_cases)
        if smtp is not None:
            smtp.send_message(msg)
        else:
            with self.connect() as s:
                s.send_message(msg)
