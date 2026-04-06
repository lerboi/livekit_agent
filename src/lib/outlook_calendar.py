"""Push appointment events to Microsoft Outlook Calendar via Graph API."""
import logging
import os
import time

import requests

from ..supabase_client import get_supabase_admin

logger = logging.getLogger("voco-agent")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"


def _refresh_outlook_token(tenant_id, creds):
    """Refresh an expired Outlook access token and persist the new one."""
    supabase = get_supabase_admin()

    resp = requests.post(TOKEN_URL, data={
        "client_id": os.environ.get("MICROSOFT_CLIENT_ID", ""),
        "client_secret": os.environ.get("MICROSOFT_CLIENT_SECRET", ""),
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token",
        "scope": "https://graph.microsoft.com/.default offline_access",
    }, timeout=10)
    resp.raise_for_status()
    tokens = resp.json()

    new_expiry = int(time.time() * 1000) + tokens.get("expires_in", 3600) * 1000

    supabase.table("calendar_credentials").update({
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", creds["refresh_token"]),
        "expiry_date": new_expiry,
    }).eq("tenant_id", tenant_id).eq("provider", "outlook").execute()

    return tokens["access_token"]


def _get_valid_token(tenant_id, creds):
    """Return a valid access token, refreshing if needed."""
    if creds.get("expiry_date") and creds["expiry_date"] > time.time() * 1000 + 300_000:
        return creds["access_token"]
    return _refresh_outlook_token(tenant_id, creds)


def push_booking_to_outlook(tenant_id, appointment_id):
    """
    Push a confirmed appointment to the tenant's Outlook calendar.
    Same best-effort pattern as google_calendar.push_booking_to_calendar().
    Failures are logged but never raised.

    NOTE: This function is synchronous and should be called via
    asyncio.to_thread() from async callers.
    """
    supabase = get_supabase_admin()

    try:
        # 1. Load Outlook credentials
        creds_response = (
            supabase.table("calendar_credentials")
            .select("access_token, refresh_token, expiry_date, calendar_id")
            .eq("tenant_id", tenant_id)
            .eq("provider", "outlook")
            .limit(1)
            .execute()
        )
        creds = creds_response.data[0] if creds_response.data else None
        if not creds:
            return  # No Outlook connected — silently skip

        # 2. Load the appointment
        appt_response = (
            supabase.table("appointments")
            .select("start_time, end_time, service_address, caller_name, urgency, notes")
            .eq("id", appointment_id)
            .limit(1)
            .execute()
        )
        appt = appt_response.data[0] if appt_response.data else None
        if not appt:
            return

        # 3. Load business name and timezone
        tenant_response = (
            supabase.table("tenants")
            .select("business_name, tenant_timezone")
            .eq("id", tenant_id)
            .limit(1)
            .execute()
        )
        tenant = tenant_response.data[0] if tenant_response.data else {}
        biz_name = tenant.get("business_name", "Appointment")
        tz = tenant.get("tenant_timezone", "America/Chicago")

        # 4. Build event
        urgency = appt.get("urgency", "routine")
        prefix = "[URGENT] " if urgency in ("emergency", "urgent") else ""
        summary = f"{prefix}{biz_name} – {appt['caller_name']}"

        description_parts = [f"Customer: {appt['caller_name']}"]
        if appt.get("service_address"):
            description_parts.append(f"Address: {appt['service_address']}")
        if urgency != "routine":
            description_parts.append(f"Urgency: {urgency}")
        if appt.get("notes"):
            description_parts.append(f"Notes: {appt['notes']}")

        event_body = {
            "subject": summary,
            "body": {"contentType": "text", "content": "\n".join(description_parts)},
            "start": {"dateTime": appt["start_time"], "timeZone": tz},
            "end": {"dateTime": appt["end_time"], "timeZone": tz},
        }
        if appt.get("service_address"):
            event_body["location"] = {"displayName": appt["service_address"]}

        # 5. Get valid token and create event
        access_token = _get_valid_token(tenant_id, creds)
        resp = requests.post(
            f"{GRAPH_BASE}/me/events",
            json=event_body,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        event = resp.json()

        # 6. Store event ID
        event_id = event.get("id")
        if event_id:
            (
                supabase.table("appointments")
                .update({"external_event_id": event_id, "external_event_provider": "outlook"})
                .eq("id", appointment_id)
                .execute()
            )

        logger.info("[agent] Outlook calendar event created: %s", event_id)

    except Exception as err:
        logger.error("[agent] Outlook calendar push failed (non-fatal): %s", str(err))
