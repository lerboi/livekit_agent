"""
check_availability tool -- real-time slot query.
Ported from src/tools/check-availability.js -- same logic, same behavior.
Now executes in-process with direct Supabase access (zero network hops).
"""

import asyncio
import logging
import time as _time
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
            "DO NOT speak the words 'available' or 'not available' for a specific time, and "
            "DO NOT quote any specific time as bookable, before invoking this tool. "
            "The tool's return is the only authoritative availability — fabricating it traps "
            "the caller in a slot that may not exist. "
            "Always speak a short filler phrase first ('Let me check that for you'), then "
            "immediately invoke this tool in the same turn. "
            "Call this tool every time the caller asks about a specific date or time — "
            "never rely on results from a previous call, and never answer about a different "
            "time based on a check you ran for an earlier time. "
            "Pass date in YYYY-MM-DD format. "
            "Pass time in HH:MM 24-hour format (e.g., '14:00' for 2 PM) to check a specific time. "
            "Always include both date AND time when the caller has named a specific hour. "
            "For vague time windows like 'afternoon,' 'morning,' or 'evening,' ask the caller "
            "for a concrete hour BEFORE calling this tool — do not pick a time on their behalf. "
            "Pass date alone only to confirm whether a day has availability at all (the tool "
            "returns a confirmation, not specific times). "
            "Omit both date and time to check the next 3 days (confirmation only)."
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
                "STATE:availability_lookup_failed reason=no_tenant"
                " | DIRECTIVE:apologize briefly; offer capture_lead so someone can call the"
                " caller back; do not retry this lookup more than once in this call."
            )

        # Reuse the tenant dict already fetched during agent init (src/agent.py)
        # and stashed on deps. Saves one Supabase round-trip (~100-200ms) per
        # slot check, which compounds: a typical call runs check_availability
        # 3-5 times as the caller explores slots. Cutting 100ms from each call
        # narrows the silence-gap window that triggers server VAD cancellations.
        tenant = deps.get("tenant")

        # Fall back to a fresh fetch only if the cached tenant is missing the
        # fields this tool needs. The agent-init fetch selects '*' so this
        # branch is normally dead; keep it as a safety net for legacy callers.
        needed_fields = ("tenant_timezone", "working_hours", "slot_duration_mins", "business_name")
        if not tenant or any(k not in tenant for k in needed_fields):
            try:
                tenant_result = await asyncio.to_thread(
                    lambda: supabase.table("tenants")
                    .select("tenant_timezone, working_hours, slot_duration_mins, business_name")
                    .eq("id", tenant_id)
                    .single()
                    .execute()
                )
                tenant = tenant_result.data if tenant_result.data else None
            except Exception as e:
                logger.error("[agent] check_availability: tenant config fetch failed: %s", e)
                return (
                    "STATE:availability_lookup_failed reason=tenant_config_error"
                    " | DIRECTIVE:apologize briefly; offer capture_lead so someone can call"
                    " the caller back; do not retry this lookup more than once in this call."
                )

        tenant_timezone = tenant.get("tenant_timezone") if tenant else None
        if not tenant_timezone:
            logger.warning(
                "[tenant_config] null tenant_timezone tenant_id=%s — falling back to UTC; "
                "caller times may be misaligned; backfill tenants.tenant_timezone to fix",
                tenant_id,
            )
            tenant_timezone = "UTC"
        slot_duration = (tenant.get("slot_duration_mins") if tenant else None) or 60

        now_iso = datetime.now(timezone.utc).isoformat()

        # Phase-fix (2026-04-23 UAT): consume the slot_cache prefetched at
        # session init if fresh (TTL 30s). Cuts this tool's latency from
        # ~500ms to ~50ms so Gemini Live's server-side function call is far
        # less likely to be cancelled by caller barge-in during the wait.
        # On cache miss/stale/error: fall through to the live-fetch path.
        _SLOT_CACHE_TTL_S = 30.0
        _slot_cache = deps.get("_slot_cache")
        appointments_data = None
        events_data = None
        zones_data = None
        buffers_data = None
        blocks_data = None

        if _slot_cache and (_time.time() - _slot_cache.get("fetched_at", 0)) < _SLOT_CACHE_TTL_S:
            appointments_data = _slot_cache.get("appointments") or []
            events_data = _slot_cache.get("calendar_events") or []
            zones_data = _slot_cache.get("service_zones") or []
            buffers_data = _slot_cache.get("zone_travel_buffers") or []
            blocks_data = _slot_cache.get("calendar_blocks") or []
            logger.info(
                "[agent] check_availability: slot_cache hit age=%.1fs "
                "appts=%d events=%d zones=%d buffers=%d blocks=%d",
                _time.time() - _slot_cache.get("fetched_at", 0),
                len(appointments_data), len(events_data), len(zones_data),
                len(buffers_data), len(blocks_data),
            )
        else:
            # Fetch live scheduling data (parallel)
            try:
                appointments_result, events_result, zones_result, buffers_result, blocks_result = await asyncio.gather(
                    asyncio.to_thread(
                        lambda: supabase.table("appointments")
                        .select("start_time, end_time, zone_id")
                        .eq("tenant_id", tenant_id)
                        .neq("status", "cancelled")
                        .neq("status", "completed")
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
                    asyncio.to_thread(
                        lambda: supabase.table("calendar_blocks")
                        .select("start_time, end_time")
                        .eq("tenant_id", tenant_id)
                        .gte("end_time", now_iso)
                        .execute()
                    ),
                )
            except Exception as e:
                logger.error("[agent] check_availability: scheduling data fetch failed: %s", e)
                return (
                    "STATE:availability_lookup_failed reason=scheduling_data_error"
                    " | DIRECTIVE:apologize briefly; offer capture_lead so someone can call the"
                    " caller back; do not retry this lookup more than once in this call. Do not"
                    " repeat this message text on-air."
                )

            appointments_data = appointments_result.data or []
            events_data = events_result.data or []
            zones_data = zones_result.data or []
            buffers_data = buffers_result.data or []
            blocks_data = blocks_result.data or []
            # Refresh the cache so subsequent tool calls within this session
            # reuse this fetch (TTL still 30s from now).
            deps["_slot_cache"] = {
                "fetched_at": _time.time(),
                "appointments": appointments_data,
                "calendar_events": events_data,
                "service_zones": zones_data,
                "zone_travel_buffers": buffers_data,
                "calendar_blocks": blocks_data,
            }

        # Determine which dates to check
        tenant_today = to_local_date_string(datetime.now(timezone.utc), tenant_timezone)
        dates_to_check: list[str] = []
        if date:
            if date < tenant_today:
                return (
                    f"STATE:date_in_past requested_date={date} tenant_today={tenant_today}"
                    " | DIRECTIVE:ask the caller for a date from today onward; do not read the"
                    " requested date back; do not fabricate times."
                )
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
                existing_bookings=appointments_data,
                external_blocks=events_data + blocks_data,
                zones=zones_data,
                zone_pair_buffers=format_zone_pair_buffers(buffers_data),
                target_date=date_str,
                tenant_timezone=tenant_timezone,
                max_slots=50,  # effectively unlimited for a normal workday
            )
            all_slots.extend(day_slots)

        # ── Specific time check: did the caller ask about a particular time? ──
        if time and date:
            requested_utc = _parse_requested_time(time, date, tenant_timezone)
            if requested_utc:
                # Reject times that are in the past or less than 1 hour from now
                now_utc = datetime.now(timezone.utc)
                min_booking_time = now_utc + timedelta(hours=1)
                if requested_utc < min_booking_time and date == tenant_today:
                    requested_speech = format_slot_for_speech(requested_utc.isoformat(), tenant_timezone)
                    return (
                        f"STATE:requested_time_too_soon requested={requested_speech}"
                        " min_notice_hours=1"
                        " | DIRECTIVE:tell the caller that time is too soon (appointments need"
                        " at least one hour's notice); ask for a later time today or another"
                        " day; do not fabricate times."
                    )
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
                    deps.setdefault("_tool_call_log", []).append({
                        "name": "check_availability",
                        "success": True,
                        "result": "available",
                        "date": date,
                        "time": time,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    })
                    return (
                        f"STATE:slot_available slot_start_utc={matched_slot['start']}"
                        f" slot_end_utc={matched_slot['end']} speech={speech_text}"
                        " | DIRECTIVE:tell the caller the requested time is available, then"
                        " ask if they want to book it. When you call book_appointment, pass"
                        " slot_start_utc VERBATIM as the slot_start parameter and slot_end_utc"
                        " VERBATIM as slot_end — do not reformat, convert, or rebuild from the"
                        " speech string (the +00:00 offset MUST be preserved). Do not read the"
                        " full slots list out loud; do not fabricate times outside this slot."
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
                            alt_lines.append(
                                f"{i + 1}. {speech} (slot_start_utc={slot['start']},"
                                f" slot_end_utc={slot['end']})"
                            )
                        alts_text = "\n".join(alt_lines)
                        deps.setdefault("_tool_call_log", []).append({
                            "name": "check_availability",
                            "success": True,
                            "result": "not_available_with_alternatives",
                            "date": date,
                            "time": time,
                            "ts": datetime.now(timezone.utc).isoformat(),
                        })
                        return (
                            f"STATE:slot_not_available requested={requested_speech}"
                            f" alternatives_count={len(closest)} date_label={date_label}"
                            f"\nALTERNATIVES:\n{alts_text}\n"
                            " | DIRECTIVE:tell the caller the requested time is not available,"
                            " then offer one or two of the alternatives above. If the caller"
                            " picks one, call book_appointment passing the alternative's"
                            " slot_start_utc VERBATIM as slot_start and slot_end_utc VERBATIM"
                            " as slot_end — do not reformat, convert, or rebuild from the"
                            " speech string (the +00:00 offset MUST be preserved). Do not read"
                            " the full alternatives list out loud; do not fabricate times"
                            " outside these slots."
                        )
                    else:
                        biz_name = (tenant.get("business_name") if tenant else None) or "the team"
                        deps.setdefault("_tool_call_log", []).append({
                            "name": "check_availability",
                            "success": True,
                            "result": "not_available_no_alternatives",
                            "date": date,
                            "time": time,
                            "ts": datetime.now(timezone.utc).isoformat(),
                        })
                        return (
                            f"STATE:no_slots_available requested={requested_speech}"
                            f" date_label={date_label} business_name={biz_name}"
                            " | DIRECTIVE:tell the caller the requested time is not available"
                            " and nothing else is open that day; ask if another day works, or"
                            " offer capture_lead so the business can call back; do not fabricate"
                            " times."
                        )

        # ── General availability: return all slots for the day(s) ──
        if len(all_slots) == 0:
            if date:
                date_label = _format_date_label(date, tenant_timezone)
            else:
                date_label = "the next few days"
            biz_name = (tenant.get("business_name") if tenant else None) or "the team"
            return (
                f"STATE:no_slots_available date_label={date_label}"
                f" business_name={biz_name}"
                " | DIRECTIVE:tell the caller nothing is open in that window; offer to check"
                " another date, or offer capture_lead so the business can call back; do not"
                " fabricate times."
            )

        # Return a clean confirmation without any specific times.
        # Specific times (earliest/latest/count) are deliberately withheld so Gemini cannot
        # mine them and present them to the caller. The AI must ask the caller to name a
        # concrete hour, then call this tool again with date+time to verify a specific slot.
        if date:
            date_label = _format_date_label(date, tenant_timezone)
        else:
            date_label = "the next few days"

        return (
            f"STATE:slots_available_unverified date_label={date_label}"
            f" slot_count={len(all_slots)}"
            " | DIRECTIVE:the day has availability but no specific slot is verified yet; ask"
            " the caller to name a concrete time (like '2 o'clock' or 'around 10 in the"
            " morning') before confirming anything is bookable; if the caller gave a vague"
            " window like 'afternoon' or 'morning,' ask them to narrow it to a specific hour;"
            " then call this tool again with both the date and the preferred time to verify"
            " that exact slot; do not read the full slots list out loud; do not fabricate"
            " times; do not mention or imply any specific times to the caller at this stage"
            " — not the earliest, not the latest, not anything in between. Do not repeat this"
            " message text on-air."
        )

    return check_availability
