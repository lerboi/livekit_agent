"""
Utility functions for the LiveKit agent.
Ported from src/utils.js -- same logic, same behavior.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


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


def _coerce_utc_aware(date: str | datetime) -> datetime:
    """
    Parse a Date/ISO string (or accept a datetime) and return a tz-aware
    datetime. Naive inputs are treated as UTC — NEVER fall through to
    Python's .astimezone() behavior that silently assumes the system
    local timezone (which on Railway is UTC but is host-dependent).

    Contract: slot_start / slot_end flowing from check_availability are
    produced as UTC ISO by slot_calculator. If any caller (Gemini, a
    legacy path) drops the offset, this function re-attaches UTC.
    """
    if isinstance(date, str):
        if date.endswith("Z"):
            date = date[:-1] + "+00:00"
        date = datetime.fromisoformat(date)
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date


def format_slot_for_speech(date: str | datetime, tz: str | None = None) -> str:
    """
    Format a UTC Date/ISO string into natural speech for AI to read aloud.
    Example: 'Tuesday March 23rd at 10:00 AM'
    """
    tz = tz or "America/Chicago"
    zoned = _coerce_utc_aware(date).astimezone(ZoneInfo(tz))
    return _format_datetime_for_speech(zoned)


def to_local_date_string(date: str | datetime, tz: str | None = None) -> str:
    """Format a Date/ISO string into a 'YYYY-MM-DD' string in the given timezone."""
    tz = tz or "America/Chicago"
    zoned = _coerce_utc_aware(date).astimezone(ZoneInfo(tz))
    return zoned.strftime("%Y-%m-%d")


def format_zone_pair_buffers(buffers: list[dict] | None) -> list[dict]:
    """Format zone_travel_buffers array -- pass through as-is."""
    return buffers or []
