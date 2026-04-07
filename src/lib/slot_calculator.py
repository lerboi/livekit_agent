"""
Slot calculator for the LiveKit agent.
Ported from src/lib/slot-calculator.js -- same logic, same behavior.

Calculates available booking slots for a given date, respecting:
- Working hours (per-day config with open/close/lunch)
- Existing bookings
- External calendar blocks
- Travel buffers between service zones
"""

import calendar
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def _parse_time(time_str: str) -> tuple[int, int]:
    """Parse a 'HH:MM' time string into (hours, minutes)."""
    parts = time_str.split(":")
    return int(parts[0]), int(parts[1])


def _local_time_to_utc(date_str: str, time_str: str, tz: str) -> datetime:
    """
    Build a UTC datetime from a local date string ('YYYY-MM-DD') and a local
    time string ('HH:MM') in the given IANA timezone.
    """
    hours, minutes = _parse_time(time_str)
    year, month, day = (int(x) for x in date_str.split("-"))
    local_dt = datetime(year, month, day, hours, minutes, 0, tzinfo=ZoneInfo(tz))
    return local_dt.astimezone(timezone.utc)


def _intervals_overlap(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> bool:
    """Check whether two half-open intervals [aStart, aEnd) and [bStart, bEnd) overlap."""
    return a_start < b_end and a_end > b_start


def _get_travel_buffer_mins(
    last_booking_zone_id: str | None,
    candidate_zone_id: str | None,
    zones: list[dict],
    zone_pair_buffers: list[dict],
) -> int:
    """
    Resolve the travel buffer in minutes between the last booking and a candidate slot.

    Logic:
    - If zones list is empty (no zones configured): flat 30-min buffer
    - If last booking has no zone_id or candidate has no zone: 30-min buffer
    - If same zone: 0-min buffer
    - If different zones: look up zone_pair_buffers; default 30-min if no entry
    """
    # No zones configured at all -- flat 30-min buffer
    if not zones or len(zones) == 0:
        return 30

    # No zone info on one or both sides -- treat as cross-zone (30min default)
    if not last_booking_zone_id or not candidate_zone_id:
        return 30

    # Same zone -- no buffer
    if last_booking_zone_id == candidate_zone_id:
        return 0

    # Different zones -- look for a custom buffer entry
    if zone_pair_buffers and len(zone_pair_buffers) > 0:
        pair = next(
            (
                p
                for p in zone_pair_buffers
                if (
                    p["zone_a_id"] == last_booking_zone_id
                    and p["zone_b_id"] == candidate_zone_id
                )
                or (
                    p["zone_a_id"] == candidate_zone_id
                    and p["zone_b_id"] == last_booking_zone_id
                )
            ),
            None,
        )
        if pair:
            return pair["buffer_mins"]

    # Default cross-zone buffer
    return 30


def _parse_iso(s: str) -> datetime:
    """Parse an ISO datetime string, handling 'Z' suffix for Python < 3.11 compat."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def calculate_available_slots(
    *,
    working_hours: dict,
    slot_duration_mins: int,
    existing_bookings: list[dict] | None = None,
    external_blocks: list[dict] | None = None,
    zones: list[dict] | None = None,
    zone_pair_buffers: list[dict] | None = None,
    target_date: str,
    tenant_timezone: str,
    max_slots: int = 10,
    candidate_zone_id: str | None = None,
) -> list[dict]:
    """
    Calculate available booking slots for a given date.

    Args:
        working_hours: Day-keyed working hours config.
        slot_duration_mins: Slot length in minutes (e.g. 60).
        existing_bookings: List of {start_time, end_time, zone_id?} (ISO strings).
        external_blocks: List of {start_time, end_time} (ISO strings).
        zones: List of {id, name} zone objects.
        zone_pair_buffers: List of {zone_a_id, zone_b_id, buffer_mins}.
        target_date: 'YYYY-MM-DD' date string.
        tenant_timezone: IANA timezone (e.g. 'America/Chicago').
        max_slots: Maximum slots to return.
        candidate_zone_id: Zone ID for the candidate booking (for buffer calc).

    Returns:
        List of {start: str, end: str} available slots as ISO strings.
    """
    if existing_bookings is None:
        existing_bookings = []
    if external_blocks is None:
        external_blocks = []
    if zones is None:
        zones = []
    if zone_pair_buffers is None:
        zone_pair_buffers = []

    # Determine the day of week from the target date
    year, month, day = (int(x) for x in target_date.split("-"))
    # Create a datetime representing midnight in the tenant timezone to get the correct weekday
    local_midnight = datetime(year, month, day, 0, 0, 0, tzinfo=ZoneInfo(tenant_timezone))
    # calendar.day_name[dt.weekday()] gives the correct English day name
    day_key = calendar.day_name[local_midnight.weekday()].lower()

    day_config = working_hours.get(day_key) if working_hours else None

    # Day off or missing config -- no slots
    if not day_config or not day_config.get("enabled"):
        return []

    open_time = day_config["open"]
    close_time = day_config["close"]
    lunch_start = day_config.get("lunchStart")
    lunch_end = day_config.get("lunchEnd")

    # Convert working hours to UTC datetime objects
    window_start = _local_time_to_utc(target_date, open_time, tenant_timezone)
    window_end = _local_time_to_utc(target_date, close_time, tenant_timezone)

    # If the entire working window is in the past, no slots are possible
    now = datetime.now(timezone.utc)
    if window_end <= now:
        return []

    # Lunch block in UTC (if configured)
    lunch_start_utc = (
        _local_time_to_utc(target_date, lunch_start, tenant_timezone)
        if lunch_start
        else None
    )
    lunch_end_utc = (
        _local_time_to_utc(target_date, lunch_end, tenant_timezone)
        if lunch_end
        else None
    )

    # Parse existing bookings to datetime objects
    parsed_bookings = [
        {
            "start": _parse_iso(b["start_time"]),
            "end": _parse_iso(b["end_time"]),
            "zone_id": b.get("zone_id") or None,
        }
        for b in existing_bookings
    ]

    # Parse external blocks to datetime objects
    parsed_blocks = [
        {
            "start": _parse_iso(b["start_time"]),
            "end": _parse_iso(b["end_time"]),
        }
        for b in external_blocks
    ]

    available: list[dict] = []
    cursor = window_start

    # Skip past slots when calculating for today -- don't offer times that have already passed
    if cursor < now < window_end:
        # Only advance if we're within today's working window
        # Check if target_date is actually today in the tenant timezone
        zoned_now = now.astimezone(ZoneInfo(tenant_timezone))
        today_str = (
            f"{zoned_now.year}-{str(zoned_now.month).zfill(2)}-{str(zoned_now.day).zfill(2)}"
        )
        if target_date == today_str:
            # Round cursor up to the next slot-grid-aligned boundary
            # so offered times match the clean grid (9:00, 10:00, etc.)
            elapsed_mins = (now - window_start).total_seconds() / 60
            slots_elapsed = math.ceil(elapsed_mins / slot_duration_mins)
            cursor = window_start + timedelta(minutes=slots_elapsed * slot_duration_mins)

    while cursor < window_end and len(available) < max_slots:
        slot_start = cursor
        slot_end = slot_start + timedelta(minutes=slot_duration_mins)

        # Slot must fit within the working window
        if slot_end > window_end:
            break

        # Skip slots that overlap with the lunch break
        if lunch_start_utc and lunch_end_utc:
            if _intervals_overlap(slot_start, slot_end, lunch_start_utc, lunch_end_utc):
                cursor = cursor + timedelta(minutes=slot_duration_mins)
                continue

        # Check overlap with existing bookings
        booked_overlap = any(
            _intervals_overlap(slot_start, slot_end, b["start"], b["end"])
            for b in parsed_bookings
        )
        if booked_overlap:
            cursor = cursor + timedelta(minutes=slot_duration_mins)
            continue

        # Check overlap with external calendar blocks
        external_overlap = any(
            _intervals_overlap(slot_start, slot_end, b["start"], b["end"])
            for b in parsed_blocks
        )
        if external_overlap:
            cursor = cursor + timedelta(minutes=slot_duration_mins)
            continue

        # Travel buffer check: find the last booking that ends before this slot starts
        bookings_before = [b for b in parsed_bookings if b["end"] <= slot_start]
        if len(bookings_before) > 0:
            # Find the one that ends latest
            last_booking = max(bookings_before, key=lambda b: b["end"])

            buffer_mins = _get_travel_buffer_mins(
                last_booking["zone_id"],
                candidate_zone_id,
                zones,
                zone_pair_buffers,
            )

            if buffer_mins > 0:
                earliest_start = last_booking["end"] + timedelta(minutes=buffer_mins)
                if slot_start < earliest_start:
                    cursor = cursor + timedelta(minutes=slot_duration_mins)
                    continue

        # Slot passes all checks
        available.append(
            {
                "start": slot_start.isoformat(),
                "end": slot_end.isoformat(),
            }
        )

        cursor = cursor + timedelta(minutes=slot_duration_mins)

    return available
