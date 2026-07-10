"""Behavior contract for the TL link email builder."""
from app.mailer import SMTPMailer, build_message


def test_message_has_recipient_sender_subject_and_link():
    msg = build_message("HR <hr@x.com>", "tl@x.com", "Ahmed", "https://app.example/?t=abc123", 3)
    assert msg["To"] == "tl@x.com"
    assert msg["From"] == "HR <hr@x.com>"
    assert "confirm" in msg["Subject"].lower()
    body = msg.get_content()
    assert "Ahmed" in body
    assert "https://app.example/?t=abc123" in body
    assert "3 attendance case" in body


def test_message_handles_missing_tl_name():
    body = build_message("hr@x.com", "tl@x.com", None, "link", 1).get_content()
    assert "Hi there," in body


def test_mailer_coerces_port_to_int():
    m = SMTPMailer("smtp-relay.brevo.com", "587", "user", "key", "hr@x.com")
    assert m.port == 587 and isinstance(m.port, int)
