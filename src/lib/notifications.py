"""
Notification service for the LiveKit agent.
Ported from src/lib/notifications.js -- same logic, same behavior.
Sends SMS via Twilio and email via Resend.
"""

import json
import logging
import os
from pathlib import Path

from twilio.rest import Client as TwilioClient
import resend

logger = logging.getLogger(__name__)

# Load message translations
_messages_dir = Path(__file__).parent.parent / "messages"
with open(_messages_dir / "en.json", "r", encoding="utf-8") as f:
    _en = json.load(f)
with open(_messages_dir / "es.json", "r", encoding="utf-8") as f:
    _es = json.load(f)

# --- Lazy-instantiated clients ------------------------------------------------

_twilio_client: TwilioClient | None = None


def _get_twilio_client() -> TwilioClient:
    global _twilio_client
    if _twilio_client is None:
        _twilio_client = TwilioClient(
            os.environ.get("TWILIO_ACCOUNT_SID"),
            os.environ.get("TWILIO_AUTH_TOKEN"),
        )
    return _twilio_client


_resend_initialized = False


def _init_resend() -> None:
    global _resend_initialized
    if not _resend_initialized:
        resend.api_key = os.environ.get("RESEND_API_KEY")
        _resend_initialized = True


# --- Interpolation helper -----------------------------------------------------


def _interpolate(template: str | None, vars: dict) -> str:
    """Replace {key} placeholders in template with values from vars dict."""
    if template is None:
        return ""
    result = template
    for key, val in vars.items():
        result = result.replace(f"{{{key}}}", val if val is not None else "")
    return result


# --- Owner SMS alert ----------------------------------------------------------


def send_owner_sms(
    *,
    to: str,
    business_name: str,
    caller_name: str | None = None,
    job_type: str | None = None,
    urgency: str | None = None,
    address: str | None = None,
    callback_link: str | None = None,
    dashboard_link: str | None = None,
):
    """Send an SMS alert to the business owner about a new call/booking."""
    is_emergency = urgency == "emergency"
    name = caller_name or "Unknown"
    job = job_type or "General inquiry"
    addr = address or "No address"

    if is_emergency:
        body = (
            f"EMERGENCY: {business_name} -- {name} needs urgent {job} at {addr}. "
            f"Call NOW: {callback_link} | Dashboard: {dashboard_link}"
        )
    else:
        body = (
            f"{business_name}: New booking -- {name}, {job} at {addr}. "
            f"Callback: {callback_link} | Dashboard: {dashboard_link}"
        )

    try:
        result = _get_twilio_client().messages.create(
            body=body,
            from_=os.environ.get("TWILIO_FROM_NUMBER"),
            to=to,
        )
        logger.info("[notifications] Owner SMS sent: %s", result.sid)
        return result
    except Exception as err:
        logger.error("[notifications] Owner SMS failed: %s", str(err))


# --- Owner email alert --------------------------------------------------------


def send_owner_email(
    *,
    to: str,
    lead: dict | None = None,
    business_name: str,
    dashboard_url: str | None = None,
):
    """Send an email alert to the business owner about a new lead."""
    lead = lead or {}
    urgency = lead.get("urgency_classification") or lead.get("urgency") or "routine"
    is_emergency = urgency == "emergency"
    caller_name = lead.get("caller_name") or "Unknown caller"

    if is_emergency:
        subject = f"EMERGENCY: New booking -- {caller_name}"
    else:
        subject = f"New booking -- {caller_name}"

    # Plain HTML email (no React Email dependency in the agent)
    html = f"""
    <h2>{"EMERGENCY" if is_emergency else "New Lead"}: {caller_name}</h2>
    <p><strong>Business:</strong> {business_name}</p>
    <p><strong>Job Type:</strong> {lead.get("job_type") or "Not specified"}</p>
    <p><strong>Address:</strong> {lead.get("service_address") or "Not provided"}</p>
    <p><strong>Phone:</strong> {lead.get("from_number") or "Unknown"}</p>
    <p><strong>Urgency:</strong> {urgency}</p>
    <p><a href="{dashboard_url}">View in Dashboard</a></p>
    """

    try:
        _init_resend()
        result = resend.Emails.send(
            {
                "from": os.environ.get("RESEND_FROM_EMAIL", "alerts@voco.live"),
                "to": to,
                "subject": subject,
                "html": html,
            }
        )
        result_id = None
        if isinstance(result, dict):
            result_id = result.get("id")
        logger.info("[notifications] Owner email sent: %s", result_id)
        return result
    except Exception as err:
        logger.error("[notifications] Owner email failed: %s", str(err))


# --- Caller recovery SMS -----------------------------------------------------


def send_caller_recovery_sms(
    *,
    to: str | None,
    from_number: str | None = None,
    caller_name: str | None = None,
    business_name: str | None = None,
    locale: str | None = None,
    urgency: str | None = None,
) -> dict:
    """Send a recovery SMS to the caller when their call couldn't be fully handled."""
    if not to:
        logger.warning("[notifications] sendCallerRecoverySMS skipped: no phone number")
        return {"success": False, "error": {"code": "NO_PHONE", "message": "No phone number provided"}}

    translations = _es if locale == "es" else _en
    is_emergency = urgency == "emergency"
    first_name = (caller_name.split(" ")[0] if caller_name else None) or "there"

    template_key = (
        "recovery_sms_attempted_emergency"
        if is_emergency
        else "recovery_sms_attempted_routine"
    )

    notifications = translations.get("notifications", {})
    body = _interpolate(
        notifications.get(template_key),
        {
            "business_name": business_name or "Your service provider",
            "first_name": first_name,
        },
    )

    try:
        result = _get_twilio_client().messages.create(
            body=body,
            from_=from_number or os.environ.get("TWILIO_FROM_NUMBER"),
            to=to,
        )
        logger.info("[notifications] Caller recovery SMS sent: %s", result.sid)
        return {"success": True, "sid": result.sid}
    except Exception as err:
        code = getattr(err, "code", "UNKNOWN")
        message = str(err)
        logger.error("[notifications] Caller recovery SMS failed: %s", message)
        return {"success": False, "error": {"code": code, "message": message}}


# --- Caller booking confirmation SMS ------------------------------------------


def send_caller_sms(
    *,
    to: str | None,
    from_number: str | None = None,
    business_name: str | None = None,
    date: str | None = None,
    time: str | None = None,
    address: str | None = None,
    locale: str | None = None,
):
    """Send a booking confirmation SMS to the caller."""
    if not to:
        logger.warning("[notifications] sendCallerSMS skipped: no phone number")
        return

    translations = _es if locale == "es" else _en
    notifications = translations.get("notifications", {})
    body = _interpolate(
        notifications.get("booking_confirmation"),
        {
            "business_name": business_name or "Your service provider",
            "date": date or "",
            "time": time or "",
            "address": address or "",
        },
    )

    try:
        result = _get_twilio_client().messages.create(
            body=body,
            from_=from_number or os.environ.get("TWILIO_FROM_NUMBER"),
            to=to,
        )
        logger.info("[notifications] Caller SMS sent: %s", result.sid)
        return result
    except Exception as err:
        logger.error("[notifications] Caller SMS failed: %s", str(err))
