"""
check_availability tool -- real-time slot query.
Ported from src/tools/check-availability.js -- same logic, same behavior.
Now executes in-process with direct Supabase access (zero network hops).
"""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from livekit.agents import function_tool
from livekit.agents.llm import RunContext

from src.lib.slot_calculator import calculate_available_slots
from src.utils import (
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


def create_check_availability_tool(deps: dict):
    @function_tool(
        name="check_availability",
        description=(
            "Check real-time appointment availability for specific dates. "
            "Use before offering slots to the caller, when the caller asks about a specific date or time, "
            "or when previously shown slots may be outdated."
        ),
    )
    async def check_availability(
        context: RunContext,
        date: str = "",
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
        tenant_result = (
            supabase.table("tenants")
            .select("tenant_timezone, working_hours, slot_duration_mins, business_name")
            .eq("id", tenant_id)
            .single()
            .execute()
        )
        tenant = tenant_result.data if tenant_result.data else None
        tenant_timezone = (tenant.get("tenant_timezone") if tenant else None) or "America/Chicago"

        now_iso = datetime.now(timezone.utc).isoformat()

        # Fetch live scheduling data
        appointments_result = (
            supabase.table("appointments")
            .select("start_time, end_time, zone_id")
            .eq("tenant_id", tenant_id)
            .neq("status", "cancelled")
            .gte("end_time", now_iso)
            .execute()
        )

        events_result = (
            supabase.table("calendar_events")
            .select("start_time, end_time")
            .eq("tenant_id", tenant_id)
            .gte("end_time", now_iso)
            .execute()
        )

        zones_result = (
            supabase.table("service_zones")
            .select("id, name, postal_codes")
            .eq("tenant_id", tenant_id)
            .execute()
        )

        buffers_result = (
            supabase.table("zone_travel_buffers")
            .select("zone_a_id, zone_b_id, buffer_mins")
            .eq("tenant_id", tenant_id)
            .execute()
        )

        # Determine which dates to check
        dates_to_check: list[str] = []
        if date:
            dates_to_check = [date]
        else:
            for day_offset in range(3):
                d = datetime.now(timezone.utc) + timedelta(days=day_offset)
                dates_to_check.append(to_local_date_string(d, tenant_timezone))

        # Calculate slots across requested dates (up to 6 total)
        all_slots: list[dict] = []
        for date_str in dates_to_check:
            if len(all_slots) >= 6:
                break

            day_slots = calculate_available_slots(
                working_hours=tenant.get("working_hours") or {} if tenant else {},
                slot_duration_mins=(tenant.get("slot_duration_mins") if tenant else None) or 60,
                existing_bookings=appointments_result.data or [],
                external_blocks=events_result.data or [],
                zones=zones_result.data or [],
                zone_pair_buffers=format_zone_pair_buffers(buffers_result.data or []),
                target_date=date_str,
                tenant_timezone=tenant_timezone,
                max_slots=6 - len(all_slots),
            )
            all_slots.extend(day_slots)

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

        # Format slots as numbered list with ISO data for booking
        slot_lines: list[str] = []
        for i, slot in enumerate(all_slots):
            speech_text = format_slot_for_speech(slot["start"], tenant_timezone)
            slot_lines.append(
                f"{i + 1}. {speech_text} (start: {slot['start']}, end: {slot['end']})"
            )

        slots_text = "\n".join(slot_lines)
        return (
            f"Available slots:\n{slots_text}\n\n"
            "Present these to the caller naturally (without the ISO times). "
            "Use the start/end values when invoking book_appointment."
        )

    return check_availability
