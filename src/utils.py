"""
Utility functions for the LiveKit agent.
Ported from src/utils.js -- same logic, same behavior.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .lib.slot_calculator import calculate_available_slots


def _ordinal(n: int) -> str:
    """Return day number with ordinal suffix (1st, 2nd, 3rd, 4th, ...)."""
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _format_datetime_for_speech(dt: datetime) -> str:
    """
    Format a datetime into the speech pattern: 'Tuesday March 4th at 10:00 AM'
    Matches date-fns format("EEEE MMMM do 'at' h:mm a").
    """
    weekday = dt.strftime("%A")  # Full weekday name
    month = dt.strftime("%B")  # Full month name
    day = _ordinal(dt.day)
    # h:mm a -- 12-hour time without leading zero on hour, AM/PM
    hour = dt.hour % 12
    if hour == 0:
        hour = 12
    minute = f"{dt.minute:02d}"
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{weekday} {month} {day} at {hour}:{minute} {ampm}"


def format_slot_for_speech(date: str | datetime, tz: str | None = None) -> str:
    """
    Format a UTC Date/ISO string into natural speech for AI to read aloud.
    Example: 'Tuesday March 23rd at 10:00 AM'
    """
    tz = tz or "America/Chicago"
    if isinstance(date, str):
        if date.endswith("Z"):
            date = date[:-1] + "+00:00"
        date = datetime.fromisoformat(date)
    zoned = date.astimezone(ZoneInfo(tz))
    return _format_datetime_for_speech(zoned)


def to_local_date_string(date: str | datetime, tz: str | None = None) -> str:
    """Format a Date/ISO string into a 'YYYY-MM-DD' string in the given timezone."""
    tz = tz or "America/Chicago"
    if isinstance(date, str):
        if date.endswith("Z"):
            date = date[:-1] + "+00:00"
        date = datetime.fromisoformat(date)
    zoned = date.astimezone(ZoneInfo(tz))
    return zoned.strftime("%Y-%m-%d")


def format_zone_pair_buffers(buffers: list[dict] | None) -> list[dict]:
    """Format zone_travel_buffers array -- pass through as-is."""
    return buffers or []


def calculate_initial_slots(supabase, tenant: dict) -> str:
    """
    Calculate initial slots for today + next 2 days (same logic as handleInbound).
    Returns formatted numbered list string.

    NOTE: This function is synchronous and should be called via
    asyncio.to_thread() from async callers.
    """
    tenant_timezone = tenant.get("tenant_timezone") or "America/Chicago"

    now_iso = datetime.now(timezone.utc).isoformat()

    # Fetch scheduling data
    appointments_result = (
        supabase.table("appointments")
        .select("start_time, end_time, zone_id")
        .eq("tenant_id", tenant["id"])
        .neq("status", "cancelled")
        .gte("end_time", now_iso)
        .execute()
    )

    events_result = (
        supabase.table("calendar_events")
        .select("start_time, end_time")
        .eq("tenant_id", tenant["id"])
        .gte("end_time", now_iso)
        .execute()
    )

    zones_result = (
        supabase.table("service_zones")
        .select("id, name, postal_codes")
        .eq("tenant_id", tenant["id"])
        .execute()
    )

    buffers_result = (
        supabase.table("zone_travel_buffers")
        .select("zone_a_id, zone_b_id, buffer_mins")
        .eq("tenant_id", tenant["id"])
        .execute()
    )

    all_slots: list[dict] = []
    for day_offset in range(3):
        if len(all_slots) >= 6:
            break

        target_date = datetime.now(timezone.utc) + timedelta(days=day_offset)
        target_date_str = to_local_date_string(target_date, tenant_timezone)

        day_slots = calculate_available_slots(
            working_hours=tenant.get("working_hours") or {},
            slot_duration_mins=tenant.get("slot_duration_mins") or 60,
            existing_bookings=appointments_result.data or [],
            external_blocks=events_result.data or [],
            zones=zones_result.data or [],
            zone_pair_buffers=format_zone_pair_buffers(buffers_result.data or []),
            target_date=target_date_str,
            tenant_timezone=tenant_timezone,
            max_slots=6 - len(all_slots),
        )
        all_slots.extend(day_slots)

    if len(all_slots) == 0:
        return ""

    lines = []
    for i, slot in enumerate(all_slots):
        iso_str = slot["start"]
        if iso_str.endswith("Z"):
            iso_str = iso_str[:-1] + "+00:00"
        zoned_start = datetime.fromisoformat(iso_str).astimezone(
            ZoneInfo(tenant_timezone)
        )
        lines.append(f"{i + 1}. {_format_datetime_for_speech(zoned_start)}")

    return "\n".join(lines)
