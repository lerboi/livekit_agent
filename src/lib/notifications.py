"""
Notification service for the LiveKit agent.
Ported from src/lib/notifications.js -- same logic, same behavior.
Sends SMS via Twilio and email via Resend.
"""

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

import resend

if TYPE_CHECKING:
    # Type-only import: lets the annotations below resolve for type checkers
    # without importing the Twilio SDK at module load (memory). The real import
    # is deferred into _get_twilio_client(). resend stays a module-level import
    # because the test suite patches `notifications.resend`.
    from twilio.rest import Client as TwilioClient

logger = logging.getLogger(__name__)

# Load message translations
_messages_dir = Path(__file__).parent.parent / "messages"
with open(_messages_dir / "en.json", "r", encoding="utf-8") as f:
    _en = json.load(f)
with open(_messages_dir / "es.json", "r", encoding="utf-8") as f:
    _es = json.load(f)

# --- Lazy-instantiated clients ------------------------------------------------

_twilio_client: "TwilioClient | None" = None


def _get_twilio_client() -> "TwilioClient":
    global _twilio_client
    if _twilio_client is None:
        # Lazy import (memory): defer the Twilio SDK out of module-load RSS.
        # Instantiation was already lazy (singleton); this makes the import lazy too.
        from twilio.rest import Client as TwilioClient
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


def build_owner_sms_body(
    *,
    business_name: str,
    caller_name: str | None = None,
    job_type: str | None = None,
    urgency: str | None = None,
    address: str | None = None,
    callback_link: str | None = None,
    dashboard_link: str | None = None,
    is_booked: bool = False,
    out_of_area: bool = False,
) -> str:
    """Render the owner-alert SMS body. Pure (no I/O) so the EXACT bytes that get
    sent are also what we persist to the owner-notification outbox on failure
    (LK-B2) — one source of truth for the wording.
    """
    is_emergency = urgency == "emergency"
    name = caller_name or "Unknown"
    job = job_type or "General inquiry"
    addr = address or "No address"
    ooa = " (OUTSIDE your area — confirm reachability)" if out_of_area else ""

    if is_emergency:
        return (
            f"EMERGENCY: {business_name} -- {name} needs urgent {job} at {addr}{ooa}. "
            f"Call NOW: {callback_link} | Dashboard: {dashboard_link}"
        )
    if is_booked:
        return (
            f"{business_name}: New booking -- {name}, {job} at {addr}{ooa}. "
            f"Callback: {callback_link} | Dashboard: {dashboard_link}"
        )
    return (
        f"{business_name}: New inquiry -- {name}, {job} at {addr}{ooa}. "
        f"Not booked — follow up. Callback: {callback_link} | Dashboard: {dashboard_link}"
    )


def send_owner_sms_body(*, to: str, from_number: str | None, body: str):
    """Low-level owner SMS send for a PRE-RENDERED body. RAISES on failure so the
    caller can persist a durable outbox row (LK-B2)."""
    try:
        result = _get_twilio_client().messages.create(
            body=body,
            from_=from_number or os.environ.get("TWILIO_FROM_NUMBER"),
            to=to,
        )
        logger.info("[notifications] Owner SMS sent: %s", result.sid)
        return result
    except Exception as err:
        logger.error("[notifications] Owner SMS failed: %s", str(err))
        raise


def send_owner_sms(
    *,
    to: str,
    from_number: str | None = None,
    business_name: str,
    caller_name: str | None = None,
    job_type: str | None = None,
    urgency: str | None = None,
    address: str | None = None,
    callback_link: str | None = None,
    dashboard_link: str | None = None,
    is_booked: bool = False,
    out_of_area: bool = False,
):
    """Send an SMS alert to the business owner about a new call/booking.

    Convenience wrapper: renders the body (build_owner_sms_body) then sends it
    (send_owner_sms_body). Preserves the legacy SWALLOW-on-failure contract
    (returns None) for any external caller. The post-call pipeline calls the
    builder + low-level sender directly so it can write a durable outbox row on
    failure (LK-B2).

    `from_number` should be the tenant's own Twilio number (the one the caller
    dialed). Falls back to the `TWILIO_FROM_NUMBER` env var for backwards
    compatibility, but each tenant has their own number so the explicit arg
    is preferred.

    `is_booked` flips the wording between "New booking" (appointment was taken)
    and "New inquiry" (caller did not book — owner follow-up needed). Emergency
    is a separate branch and always takes the urgent-callback wording regardless
    of booking outcome.

    `out_of_area` (M16 P1, Capability A) appends a short flag when the caller's
    confirmed address was outside the tenant's Service Area, so the owner knows
    to confirm reachability before scheduling.
    """
    body = build_owner_sms_body(
        business_name=business_name,
        caller_name=caller_name,
        job_type=job_type,
        urgency=urgency,
        address=address,
        callback_link=callback_link,
        dashboard_link=dashboard_link,
        is_booked=is_booked,
        out_of_area=out_of_area,
    )
    try:
        return send_owner_sms_body(to=to, from_number=from_number, body=body)
    except Exception:
        return None  # legacy swallow contract preserved for external callers


# --- Owner email alert --------------------------------------------------------


def build_owner_email_content(
    *,
    lead: dict | None = None,
    business_name: str,
    dashboard_url: str | None = None,
    is_booked: bool = False,
) -> tuple[str, str]:
    """Render (subject, html) for the owner-alert email. Pure (no I/O) so the
    persisted outbox payload matches exactly what gets sent (LK-B2).

    `is_booked` flips the subject and heading between "New booking" (caller
    took an appointment) and "New inquiry" (caller did not book — follow-up
    needed). Emergency always takes the EMERGENCY wording regardless.
    """
    lead = lead or {}
    urgency = lead.get("urgency_classification") or lead.get("urgency") or "routine"
    is_emergency = urgency == "emergency"
    caller_name = lead.get("caller_name") or "Unknown caller"

    if is_emergency:
        subject = f"EMERGENCY: {'New booking' if is_booked else 'New inquiry'} -- {caller_name}"
        heading_label = "EMERGENCY"
    elif is_booked:
        subject = f"New booking -- {caller_name}"
        heading_label = "New booking"
    else:
        subject = f"New inquiry -- {caller_name}"
        heading_label = "New inquiry (not booked — follow up)"

    # M16 P1 (Capability A): highlight an out-of-area lead so the owner confirms
    # reachability before scheduling. The flag is set on the lead dict upstream.
    ooa_html = (
        '<p style="color:#b91c1c;font-weight:bold;">'
        "⚠ OUTSIDE your service area — confirm you can reach this address "
        "before scheduling.</p>"
        if lead.get("out_of_area")
        else ""
    )

    # Plain HTML email (no React Email dependency in the agent)
    html = f"""
    <h2>{heading_label}: {caller_name}</h2>
    <p><strong>Business:</strong> {business_name}</p>
    <p><strong>Job Type:</strong> {lead.get("job_type") or "Not specified"}</p>
    <p><strong>Address:</strong> {lead.get("service_address") or "Not provided"}</p>
    <p><strong>Phone:</strong> {lead.get("from_number") or "Unknown"}</p>
    <p><strong>Urgency:</strong> {urgency}</p>
    {ooa_html}
    <p><a href="{dashboard_url}">View in Dashboard</a></p>
    """
    return subject, html


def send_owner_email_content(*, to: str, subject: str, html: str):
    """Low-level owner email send for PRE-RENDERED subject/html. RAISES on failure
    so the caller can persist a durable outbox row (LK-B2)."""
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
        result_id = result.get("id") if isinstance(result, dict) else None
        logger.info("[notifications] Owner email sent: %s", result_id)
        return result
    except Exception as err:
        logger.error("[notifications] Owner email failed: %s", str(err))
        raise


def send_owner_email(
    *,
    to: str,
    lead: dict | None = None,
    business_name: str,
    dashboard_url: str | None = None,
    is_booked: bool = False,
):
    """Send an email alert to the business owner about a new lead.

    Convenience wrapper: renders (subject, html) then sends. Preserves the legacy
    SWALLOW-on-failure contract (returns None); the post-call pipeline uses
    build_owner_email_content + send_owner_email_content directly so it can write
    a durable outbox row on failure (LK-B2).
    """
    subject, html = build_owner_email_content(
        lead=lead,
        business_name=business_name,
        dashboard_url=dashboard_url,
        is_booked=is_booked,
    )
    try:
        return send_owner_email_content(to=to, subject=subject, html=html)
    except Exception:
        return None  # legacy swallow contract preserved for external callers


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
