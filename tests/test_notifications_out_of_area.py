"""Owner-notification out-of-area note (M16 P1, Capability A).

When the caller's confirmed address is outside the tenant's Service Area, the
owner SMS gets a short "(OUTSIDE your area — confirm reachability)" suffix and
the owner email gets a highlighted warning block. Both are silent when the
address is in-area. Twilio + Resend clients are mocked — no live sends.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.lib.notifications import send_owner_sms, send_owner_email


def _sms_body(out_of_area: bool, *, is_booked: bool = False, urgency: str = "routine") -> str:
    with patch("src.lib.notifications._get_twilio_client") as mock_client:
        send_owner_sms(
            to="+15550000000",
            from_number="+15551112222",
            business_name="Acme Plumbing",
            caller_name="Alice",
            job_type="leak repair",
            urgency=urgency,
            address="123 Main St",
            callback_link="tel:+15553334444",
            dashboard_link="https://app/dashboard",
            is_booked=is_booked,
            out_of_area=out_of_area,
        )
        return mock_client.return_value.messages.create.call_args.kwargs["body"]


def test_owner_sms_appends_ooa_note_when_out_of_area():
    body = _sms_body(True)
    assert "OUTSIDE your area" in body
    assert "confirm reachability" in body


def test_owner_sms_no_note_when_in_area():
    assert "OUTSIDE your area" not in _sms_body(False)


def test_owner_sms_ooa_note_on_booked_and_emergency():
    assert "OUTSIDE your area" in _sms_body(True, is_booked=True)
    assert "OUTSIDE your area" in _sms_body(True, urgency="emergency")


def _email_html(lead: dict) -> str:
    with patch("src.lib.notifications.resend") as mock_resend:
        mock_resend.Emails.send.return_value = {"id": "email-1"}
        send_owner_email(
            to="owner@acme.test",
            lead=lead,
            business_name="Acme Plumbing",
            dashboard_url="https://app/dashboard",
        )
        return mock_resend.Emails.send.call_args.args[0]["html"]


def test_owner_email_includes_ooa_block_when_out_of_area():
    html = _email_html({"caller_name": "Alice", "out_of_area": True})
    assert "OUTSIDE your service area" in html


def test_owner_email_no_ooa_block_when_in_area():
    html = _email_html({"caller_name": "Alice", "out_of_area": False})
    assert "OUTSIDE your service area" not in html


def test_owner_email_no_ooa_block_when_flag_absent():
    html = _email_html({"caller_name": "Alice"})
    assert "OUTSIDE your service area" not in html
