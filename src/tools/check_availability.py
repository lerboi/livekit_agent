"""
check_availability tool -- real-time slot query.
Ported from src/tools/check-availability.js -- same logic, same behavior.
Now executes in-process with direct Supabase access (zero network hops).
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from livekit.agents import function_tool, RunContext

from ..lib.slot_calculator import calculate_available_slots
from ..utils import (
    format_slot_for_speech,
    to_local_date_string,
    format_zone_pair_buffers,
)

logger = logging.getLogger(__name__)


def _ordinal(n: int) -> str:
    """Return day number with ordinal suffix (1st, 2nd, 3rd, 4th, ...)."""
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _format_date_label(date_str: str, tenant_timezone: str) -> str:
    """Format a YYYY-MM-DD date string into 'Tuesday, March 4th' for display."""
    year, month, day = (int(x) for x in date_str.split("-"))
    dt = datetime(year, month, day, 12, 0, 0, tzinfo=ZoneInfo(tenant_timezone))
    weekday = dt.strftime("%A")
    month_name = dt.strftime("%B")
    return f"{weekday}, {month_name} {_ordinal(dt.day)}"


def _parse_requested_time(time_str: str, date_str: str, tenant_timezone: str) -> datetime | None:
    """Parse a time string like '14:00', '2:00 PM', '2pm' into a UTC datetime for the given date."""
    import re as _re

    time_str = time_str.strip().lower()

    # Try HH:MM 24-hour format
    match = _re.match(r"^(\d{1,2}):(\d{2})$", time_str)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
    else:
        # Try 12-hour formats: "2:00 PM", "2pm", "2 pm", "14"
        match = _re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", time_str)
        if not match:
            return None
        hour = int(match.group(1))
        minute = int(match.group(2)) if match.group(2) else 0
        ampm = match.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0

    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None

    year, month, day = (int(x) for x in date_str.split("-"))
    local_dt = datetime(year, month, day, hour, minute, 0, tzinfo=ZoneInfo(tenant_timezone))
    return local_dt.astimezone(timezone.utc)


def create_check_availability_tool(deps: dict):
    @function_tool(
        name="check_availability",
        description=(
            "Check real-time appointment availability. "
            "Always tell the caller you're checking before calling this tool. "
            "Call this tool every time the caller asks about a specific date or time — "
            "never rely on results from a previous call. "
            "Pass date in YYYY-MM-DD format. "
            "Pass time in HH:MM 24-hour format (e.g., '14:00' for 2 PM) to check a specific time. "
            "Omit time to get all available slots for the day. "
            "Omit both date and time to check the next 3 days."
        ),
    )
    async def check_availability(
        context: RunContext,
        date: str = "",
        time: str = "",
        urgency: str = "routine",
    ) -> str:
        tenant_id = deps.get("tenant_id")
        supabase = deps["supabase"]

        if not tenant_id:
            return (
                "I was unable to check availability right now. "
                "Let me take your information and someone will call you back to schedule."
            )

        # Fetch tenant config
        tenant_result = await asyncio.to_thread(
            lambda: supabase.table("tenants")
            .select("tenant_timezone, working_hours, slot_duration_mins, business_name")
            .eq("id", tenant_id)
            .single()
            .execute()
        )
        tenant = tenant_result.data if tenant_result.data else None
        tenant_timezone = (tenant.get("tenant_timezone") if tenant else None) or "America/Chicago"
        slot_duration = (tenant.get("slot_duration_mins") if tenant else None) or 60

        now_iso = datetime.now(timezone.utc).isoformat()

        # Fetch live scheduling data (parallel)
        appointments_result, events_result, zones_result, buffers_result = await asyncio.gather(
            asyncio.to_thread(
                lambda: supabase.table("appointments")
                .select("start_time, end_time, zone_id")
                .eq("tenant_id", tenant_id)
                .neq("status", "cancelled")
                .gte("end_time", now_iso)
                .execute()
            ),
            asyncio.to_thread(
                lambda: supabase.table("calendar_events")
                .select("start_time, end_time")
                .eq("tenant_id", tenant_id)
                .gte("end_time", now_iso)
                .execute()
            ),
            asyncio.to_thread(
                lambda: supabase.table("service_zones")
                .select("id, name, postal_codes")
                .eq("tenant_id", tenant_id)
                .execute()
            ),
            asyncio.to_thread(
                lambda: supabase.table("zone_travel_buffers")
                .select("zone_a_id, zone_b_id, buffer_mins")
                .eq("tenant_id", tenant_id)
                .execute()
            ),
        )

        # Determine which dates to check
        dates_to_check: list[str] = []
        if date:
            dates_to_check = [date]
        else:
            for day_offset in range(3):
                d = datetime.now(timezone.utc) + timedelta(days=day_offset)
                dates_to_check.append(to_local_date_string(d, tenant_timezone))

        # Calculate ALL slots across requested dates (no artificial cap)
        all_slots: list[dict] = []
        for date_str in dates_to_check:
            day_slots = calculate_available_slots(
                working_hours=tenant.get("working_hours") or {} if tenant else {},
                slot_duration_mins=slot_duration,
                existing_bookings=appointments_result.data or [],
                external_blocks=events_result.data or [],
                zones=zones_result.data or [],
                zone_pair_buffers=format_zone_pair_buffers(buffers_result.data or []),
                target_date=date_str,
                tenant_timezone=tenant_timezone,
                max_slots=50,  # effectively unlimited for a normal workday
            )
            all_slots.extend(day_slots)

        # ── Specific time check: did the caller ask about a particular time? ──
        if time and date:
            requested_utc = _parse_requested_time(time, date, tenant_timezone)
            if requested_utc:
                requested_end = requested_utc + timedelta(minutes=slot_duration)

                # Check if the requested time falls within any available slot
                matched_slot = None
                for slot in all_slots:
                    slot_start = datetime.fromisoformat(
                        slot["start"].replace("Z", "+00:00") if slot["start"].endswith("Z") else slot["start"]
                    )
                    slot_end = datetime.fromisoformat(
                        slot["end"].replace("Z", "+00:00") if slot["end"].endswith("Z") else slot["end"]
                    )
                    # Match if the requested time aligns with a slot start,
                    # or falls within a slot window
                    if slot_start <= requested_utc < slot_end:
                        matched_slot = slot
                        break

                if matched_slot:
                    speech_text = format_slot_for_speech(matched_slot["start"], tenant_timezone)
                    return (
                        f"Yes, {speech_text} is available. "
                        f"(start: {matched_slot['start']}, end: {matched_slot['end']})\n"
                        "If the caller wants this slot, proceed to book using the start/end values above."
                    )
                else:
                    # Not available — find the closest alternatives
                    nearby: list[dict] = []
                    for slot in all_slots:
                        slot_start = datetime.fromisoformat(
                            slot["start"].replace("Z", "+00:00") if slot["start"].endswith("Z") else slot["start"]
                        )
                        slot["_distance"] = abs((slot_start - requested_utc).total_seconds())
                        nearby.append(slot)

                    nearby.sort(key=lambda s: s["_distance"])
                    closest = nearby[:3]

                    date_label = _format_date_label(date, tenant_timezone)
                    requested_speech = format_slot_for_speech(requested_utc.isoformat(), tenant_timezone)

                    if closest:
                        alt_lines = []
                        for i, slot in enumerate(closest):
                            speech = format_slot_for_speech(slot["start"], tenant_timezone)
                            alt_lines.append(f"{i + 1}. {speech} (start: {slot['start']}, end: {slot['end']})")
                        alts_text = "\n".join(alt_lines)
                        return (
                            f"{requested_speech} is not available. "
                            f"The closest available times on {date_label} are:\n{alts_text}\n\n"
                            "Offer the caller these alternatives. "
                            "Use the start/end values when invoking book_appointment."
                        )
                    else:
                        biz_name = (tenant.get("business_name") if tenant else None) or "the team"
                        return (
                            f"{requested_speech} is not available, and there are no other "
                            f"slots on {date_label}. Ask the caller if another day works, "
                            f"or capture their information so {biz_name} can call back."
                        )

        # ── General availability: return all slots for the day(s) ──
        if len(all_slots) == 0:
            if date:
                date_label = _format_date_label(date, tenant_timezone)
            else:
                date_label = "the next few days"
            biz_name = (tenant.get("business_name") if tenant else None) or "the team"
            return (
                f"No available slots for {date_label}. "
                f"Ask the caller if another date works, or capture their information "
                f"so {biz_name} can call back to schedule."
            )

        slot_lines: list[str] = []
        for i, slot in enumerate(all_slots):
            speech_text = format_slot_for_speech(slot["start"], tenant_timezone)
            slot_lines.append(
                f"{i + 1}. {speech_text} (start: {slot['start']}, end: {slot['end']})"
            )

        slots_text = "\n".join(slot_lines)
        return (
            f"Available slots:\n{slots_text}\n\n"
            "Do NOT read all these slots to the caller. "
            "If the caller's preferred time is in the list, confirm it and proceed to book. "
            "If not, offer only the 2-3 closest alternatives to what they asked for. "
            "Use the start/end values when invoking book_appointment."
        )

    return check_availability
