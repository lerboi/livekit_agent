"""Dispatch calendar push to the correct provider (Google or Outlook)."""
import logging

from ..supabase_client import get_supabase_admin
from .google_calendar import push_booking_to_calendar as push_google
from .outlook_calendar import push_booking_to_outlook

logger = logging.getLogger("voco-agent")


def push_booking_to_calendar(tenant_id, appointment_id):
    """
    Push a booking to the tenant's connected calendar (Google or Outlook).
    Looks up the primary calendar credential, falls back to any credential,
    then dispatches to the correct provider.

    NOTE: This function is synchronous and should be called via
    asyncio.to_thread() from async callers.
    """
    supabase = get_supabase_admin()

    try:
        result = (
            supabase.table("calendar_credentials")
            .select("provider")
            .eq("tenant_id", tenant_id)
            .eq("is_primary", True)
            .limit(1)
            .execute()
        )
        cred = result.data[0] if result.data else None

        if not cred:
            # No primary calendar — check for any credential
            fallback = (
                supabase.table("calendar_credentials")
                .select("provider")
                .eq("tenant_id", tenant_id)
                .limit(1)
                .execute()
            )
            cred = fallback.data[0] if fallback.data else None

        if not cred:
            return  # No calendar connected at all

        if cred["provider"] == "google":
            push_google(tenant_id, appointment_id)
        elif cred["provider"] == "outlook":
            push_booking_to_outlook(tenant_id, appointment_id)
        else:
            logger.warning("[agent] Unknown calendar provider: %s", cred["provider"])

    except Exception as err:
        logger.error("[agent] Calendar dispatch failed (non-fatal): %s", str(err))
