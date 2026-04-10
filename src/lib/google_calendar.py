"""
Google Calendar integration for the LiveKit agent.
Ported from src/lib/google-calendar.js -- same logic, same behavior.
Lightweight adapter -- pushes bookings to Google Calendar.
"""

import logging
import os
from datetime import timezone

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from ..supabase_client import get_supabase_admin

logger = logging.getLogger(__name__)


def push_booking_to_calendar(tenant_id: str, appointment_id: str) -> None:
    """
    Push a booking to Google Calendar.
    Same logic as src/lib/google-calendar.js pushBookingToCalendar().
    This is a best-effort operation -- failures are logged but never raised.

    NOTE: This function is synchronous and should be called via
    asyncio.to_thread() from async callers.
    """
    supabase = get_supabase_admin()

    try:
        # Check if tenant has calendar credentials
        creds_response = (
            supabase.table("calendar_credentials")
            .select("access_token, refresh_token, expiry_date, calendar_id")
            .eq("tenant_id", tenant_id)
            .eq("provider", "google")
            .limit(1)
            .execute()
        )

        creds = creds_response.data[0] if creds_response.data else None

        if not creds:
            # No calendar configured -- silently skip
            return

        # Fetch the appointment details
        appointment_response = (
            supabase.table("appointments")
            .select("start_time, end_time, service_address, caller_name, urgency, notes")
            .eq("id", appointment_id)
            .limit(1)
            .execute()
        )

        appointment = (
            appointment_response.data[0] if appointment_response.data else None
        )
        if not appointment:
            return

        # Fetch tenant business name
        tenant_response = (
            supabase.table("tenants")
            .select("business_name")
            .eq("id", tenant_id)
            .limit(1)
            .execute()
        )

        tenant = tenant_response.data[0] if tenant_response.data else None

        is_urgent = appointment.get("urgency") == "emergency"
        title_prefix = "[URGENT] " if is_urgent else ""
        caller = appointment.get("caller_name") or "Customer"
        biz_name = tenant.get("business_name") if tenant else "Appointment"
        summary = f"{title_prefix}{caller} - {biz_name}"

        # Build Google OAuth2 credentials
        expiry_date = creds.get("expiry_date")
        google_creds = Credentials(
            token=creds["access_token"],
            refresh_token=creds.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ.get("GOOGLE_CLIENT_ID"),
            client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
        )

        service = build("calendar", "v3", credentials=google_creds)

        # Build description lines
        description_parts = [
            f"Service Address: {appointment.get('service_address') or 'TBD'}",
            f"Urgency: {appointment.get('urgency')}",
        ]
        if appointment.get("notes"):
            description_parts.append(f"Notes: {appointment['notes']}")

        description = "\n".join(part for part in description_parts if part)

        event_body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": appointment["start_time"]},
            "end": {"dateTime": appointment["end_time"]},
        }

        event = (
            service.events()
            .insert(
                calendarId=creds.get("calendar_id") or "primary",
                body=event_body,
            )
            .execute()
        )

        # Persist refreshed tokens if googleapiclient auto-refreshed them during the
        # insert request. Without this, each booking pays the refresh cost and the DB
        # stays permanently stale, eventually hitting Google's refresh rate limit and
        # silently breaking calendar sync for this tenant.
        try:
            new_token = getattr(google_creds, "token", None)
            if new_token and new_token != creds["access_token"]:
                update_payload = {"access_token": new_token}
                # google_creds.expiry is a naive UTC datetime; calendar_credentials
                # stores expiry_date as Unix milliseconds (bigint) — see migration 003.
                new_expiry = getattr(google_creds, "expiry", None)
                if new_expiry is not None:
                    update_payload["expiry_date"] = int(
                        new_expiry.replace(tzinfo=timezone.utc).timestamp() * 1000
                    )
                # Google occasionally rotates refresh tokens; persist if changed.
                new_refresh = getattr(google_creds, "refresh_token", None)
                if new_refresh and new_refresh != creds.get("refresh_token"):
                    update_payload["refresh_token"] = new_refresh
                (
                    supabase.table("calendar_credentials")
                    .update(update_payload)
                    .eq("tenant_id", tenant_id)
                    .eq("provider", "google")
                    .execute()
                )
                logger.info(
                    "[agent] Persisted refreshed Google credentials for tenant=%s",
                    tenant_id,
                )
        except Exception as persist_err:
            logger.error(
                "[agent] Failed to persist refreshed Google credentials (non-fatal): %s",
                persist_err,
            )

        # Store the Google event ID
        event_id = event.get("id")
        if event_id:
            (
                supabase.table("appointments")
                .update({"external_event_id": event_id, "external_event_provider": "google"})
                .eq("id", appointment_id)
                .execute()
            )

        logger.info("[agent] Calendar event created: %s", event_id)

    except Exception as err:
        logger.error("[agent] Calendar push failed (non-fatal): %s", str(err))
