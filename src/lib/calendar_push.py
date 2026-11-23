"""Dispatch calendar push to the correct provider (Google or Outlook)."""
import logging

from ..supabase_client import get_supabase_admin
from .google_calendar import push_booking_to_calendar as push_google
from .outlook_calendar import push_booking_to_outlook

logger = logging.getLogger("voco-agent")

_PROVIDERS = ("google", "outlook")


def _resolve_provider(supabase, tenant_id, provider):
    """Which connected calendar to push to, or None.

    An explicit provider (the Demo Studio's "Save to" choice) is used only if
    the tenant has that provider connected; it never falls back to another
    one. Without it: the primary credential, then any credential.
    """
    if provider:
        if provider not in _PROVIDERS:
            logger.warning("[agent] Unknown calendar provider: %s", provider)
            return None
        result = (
            supabase.table("calendar_credentials")
            .select("provider")
            .eq("tenant_id", tenant_id)
            .eq("provider", provider)
            .limit(1)
            .execute()
        )
        if not result.data:
            logger.warning(
                "[agent] Calendar push asked for provider=%s but tenant=%s has no such connection",
                provider, tenant_id,
            )
            return None
        return provider

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

    return cred["provider"] if cred else None


def push_booking_to_calendar(tenant_id, appointment_id, tenant_timezone, provider=None, style=None):
    """
    Push a booking to the tenant's connected calendar (Google or Outlook).

    provider: None = the tenant's primary calendar (falling back to any
    connected one); "google" / "outlook" = exactly that connection.
    style: "scheduling" = scheduling-profile event wording (see calendar_text).

    Returns {"provider", "event_id", "html_link"} when an event was created,
    else None. Best-effort: failures are logged, never raised.

    NOTE: This function is synchronous and should be called via
    asyncio.to_thread() from async callers.
    """
    supabase = get_supabase_admin()

    try:
        resolved = _resolve_provider(supabase, tenant_id, provider)
        if not resolved:
            return None  # No (matching) calendar connected

        if resolved == "google":
            return push_google(tenant_id, appointment_id, tenant_timezone, style=style)
        if resolved == "outlook":
            return push_booking_to_outlook(tenant_id, appointment_id, style=style)
        logger.warning("[agent] Unknown calendar provider: %s", resolved)
        return None

    except Exception as err:
        logger.error("[agent] Calendar dispatch failed (non-fatal): %s", str(err))
        return None
