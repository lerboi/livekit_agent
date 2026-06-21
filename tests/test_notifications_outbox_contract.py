"""LK-B2 owner-notification builder + send-contract tests.

The post-call pipeline (post_call.py §7) renders the message body with the PURE
builders, sends it with the LOW-LEVEL senders (which RAISE on failure so a durable
owner_notification_failures outbox row can be written), and the retry cron re-sends
the stored payload verbatim. The legacy convenience wrappers must still SWALLOW
(return None) so external callers are unaffected.

Twilio + Resend clients are mocked — no live sends.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.lib.notifications import (
    build_owner_sms_body,
    build_owner_email_content,
    send_owner_sms_body,
    send_owner_email_content,
    send_owner_sms,
    send_owner_email,
)


# ---------- Pure builders ----------


def test_build_sms_body_emergency():
    body = build_owner_sms_body(
        business_name="Acme", caller_name="Alice", job_type="burst pipe",
        urgency="emergency", address="1 Main St",
        callback_link="tel:+1", dashboard_link="https://d",
    )
    assert body.startswith("EMERGENCY: Acme")
    assert "burst pipe" in body and "Call NOW" in body


def test_build_sms_body_booked_vs_inquiry():
    booked = build_owner_sms_body(
        business_name="Acme", caller_name="Bob", job_type="leak",
        urgency="routine", address="2 Main St",
        callback_link="tel:+1", dashboard_link="https://d", is_booked=True,
    )
    inquiry = build_owner_sms_body(
        business_name="Acme", caller_name="Bob", job_type="leak",
        urgency="routine", address="2 Main St",
        callback_link="tel:+1", dashboard_link="https://d", is_booked=False,
    )
    assert "New booking" in booked
    assert "New inquiry" in inquiry and "follow up" in inquiry


def test_build_email_content_returns_subject_and_html():
    subject, html = build_owner_email_content(
        lead={"caller_name": "Carol", "job_type": "HVAC", "urgency": "routine"},
        business_name="Acme", dashboard_url="https://d",
    )
    assert "Carol" in subject
    assert "<h2>" in html and "Acme" in html


# ---------- Low-level senders RAISE on failure (outbox depends on this) ----------


def test_send_sms_body_raises_on_failure():
    with patch("src.lib.notifications._get_twilio_client") as mock_client:
        mock_client.return_value.messages.create.side_effect = RuntimeError("twilio down")
        with pytest.raises(RuntimeError):
            send_owner_sms_body(to="+15550000000", from_number="+15551112222", body="hi")


def test_send_email_content_raises_on_failure():
    with patch("src.lib.notifications.resend") as mock_resend:
        mock_resend.Emails.send.side_effect = RuntimeError("resend down")
        with pytest.raises(RuntimeError):
            send_owner_email_content(to="o@a.test", subject="s", html="<p>h</p>")


def test_send_sms_body_returns_result_on_success():
    with patch("src.lib.notifications._get_twilio_client") as mock_client:
        mock_client.return_value.messages.create.return_value = MagicMock(sid="SM1")
        result = send_owner_sms_body(to="+15550000000", from_number="+1", body="hi")
        assert result.sid == "SM1"


# ---------- Convenience wrappers SWALLOW on failure (legacy contract) ----------


def test_send_owner_sms_wrapper_swallows_and_returns_none():
    with patch("src.lib.notifications._get_twilio_client") as mock_client:
        mock_client.return_value.messages.create.side_effect = RuntimeError("twilio down")
        out = send_owner_sms(
            to="+15550000000", from_number="+1", business_name="Acme",
            caller_name="Al", job_type="leak", urgency="routine",
        )
        assert out is None


def test_send_owner_email_wrapper_swallows_and_returns_none():
    with patch("src.lib.notifications.resend") as mock_resend:
        mock_resend.Emails.send.side_effect = RuntimeError("resend down")
        out = send_owner_email(
            to="o@a.test", lead={"caller_name": "Al"}, business_name="Acme",
            dashboard_url="https://d",
        )
        assert out is None
