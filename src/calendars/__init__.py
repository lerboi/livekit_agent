"""External calendars the appointments agent writes to.

One interface, two providers. A connection object exposes::

    prepare()                                   warm the access token
    create_event(title, body, start_utc, end_utc, tz) -> {provider, event_id, html_link}
    move_event(event_id, start_utc, end_utc, tz)      -> {provider, event_id, html_link}
    delete_event(event_id)

Failures raise ``CalendarError``. Everything is synchronous; callers use
``asyncio.to_thread``.

``calendar_target`` is the sandbox rule in one place: which calendar, if any,
a call is allowed to write to.
"""

from __future__ import annotations

import logging

from .google import CalendarError, GoogleCalendar
from .outlook import OutlookCalendar

logger = logging.getLogger("voco-agent")

PROVIDERS = {"google": GoogleCalendar, "outlook": OutlookCalendar}

__all__ = ["CalendarError", "PROVIDERS", "calendar_target", "open_calendar"]

_CRED_COLUMNS = "provider, access_token, refresh_token, expiry_date, calendar_id, is_primary"


def calendar_target(is_test_call: bool, demo: dict | None) -> tuple[bool, str | None]:
    """(may_write, provider) for a call.

    * production call: (True, None), meaning the tenant's primary calendar.
    * Demo Studio call naming an external destination: (True, that provider),
      and only that one.
    * any other test call: (False, None). Test calls never touch an external
      calendar.
    """
    if not is_test_call:
        return True, None
    destination = (demo or {}).get("destination")
    if destination in PROVIDERS:
        return True, destination
    return False, None


def open_calendar(supabase, tenant_id: str, provider: str | None = None):
    """Connection for the tenant's calendar, or None when there is none.

    ``provider`` given: exactly that connection, never another one. Without
    it: the primary calendar, else any connected calendar.
    """
    if not tenant_id:
        return None
    if provider is not None and provider not in PROVIDERS:
        logger.warning("[calendar] unknown provider %r", provider)
        return None
    result = (
        supabase.table("calendar_credentials")
        .select(_CRED_COLUMNS)
        .eq("tenant_id", tenant_id)
        .execute()
    )
    rows = [r for r in (result.data or []) if r.get("provider") in PROVIDERS]
    if provider is not None:
        rows = [r for r in rows if r["provider"] == provider]
    if not rows:
        return None
    row = next((r for r in rows if r.get("is_primary")), rows[0])
    return PROVIDERS[row["provider"]](tenant_id, row, supabase)
