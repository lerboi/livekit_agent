"""
book_appointment tool -- atomic slot booking.
Ported from src/tools/book-appointment.js -- same logic, same behavior.
All side effects (calendar sync, SMS, recovery SMS) run in-process.
"""

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from livekit.agents import function_tool, RunContext

from ..lib.booking import atomic_book_slot
from ..lib.slot_calculator import calculate_available_slots
from ..lib.notifications import send_caller_sms, send_caller_recovery_sms
from ..lib.calendar_push import push_booking_to_calendar
from ..utils import (
    format_slot_for_speech,
    to_local_date_string,
    format_zone_pair_buffers,
)

logger = logging.getLogger(__name__)


def _format_date_for_sms(iso_str: str, tenant_timezone: str) -> str:
    """Format ISO datetime to 'Tuesday, March 4th' for SMS."""
    if iso_str.endswith("Z"):
        iso_str = iso_str[:-1] + "+00:00"
    dt = datetime.fromisoformat(iso_str).astimezone(ZoneInfo(tenant_timezone))
    weekday = dt.strftime("%A")
    month = dt.strftime("%B")
    day = dt.day
    if 11 <= (day % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{weekday}, {month} {day}{suffix}"


def _format_time_for_sms(iso_str: str, tenant_timezone: str) -> str:
    """Format ISO datetime to 'h:mm AM/PM' for SMS."""
    if iso_str.endswith("Z"):
        iso_str = iso_str[:-1] + "+00:00"
    dt = datetime.fromisoformat(iso_str).astimezone(ZoneInfo(tenant_timezone))
    hour = dt.hour % 12
    if hour == 0:
        hour = 12
    minute = f"{dt.minute:02d}"
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{hour}:{minute} {ampm}"


async def _send_recovery_sms(deps: dict, tenant: dict | None, urgency: str, caller_name: str) -> None:
    """Send recovery SMS on failed booking -- same logic as JS sendRecoverySMS."""
    supabase = deps["supabase"]
    call_id = deps.get("call_id", "")

    try:
        locale = (tenant.get("default_locale") if tenant else None) or "en"

        # Write pending status
        await asyncio.to_thread(
            lambda: supabase.table("calls").update(
                {
                    "recovery_sms_status": "pending",
                    "recovery_sms_last_attempt_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("call_id", call_id).execute()
        )

        delivery_result = await asyncio.to_thread(
            lambda: send_caller_recovery_sms(
                to=deps.get("from_number"),
                from_number=deps.get("to_number"),
                caller_name=caller_name,
                business_name=(tenant.get("business_name") if tenant else None) or "Your service provider",
                locale=locale,
                urgency=urgency or "routine",
            )
        )

        # Write delivery result
        if delivery_result.get("success"):
            await asyncio.to_thread(
                lambda: supabase.table("calls").update(
                    {
                        "recovery_sms_status": "sent",
                        "recovery_sms_retry_count": 0,
                        "recovery_sms_last_error": None,
                        "recovery_sms_last_attempt_at": datetime.now(timezone.utc).isoformat(),
                        "recovery_sms_sent_at": datetime.now(timezone.utc).isoformat(),
                    }
                ).eq("call_id", call_id).execute()
            )
        else:
            error = delivery_result.get("error", {})
            await asyncio.to_thread(
                lambda: supabase.table("calls").update(
                    {
                        "recovery_sms_status": "retrying",
                        "recovery_sms_retry_count": 1,
                        "recovery_sms_last_error": f"{error.get('code', 'UNKNOWN')}: {error.get('message', '')}",
                        "recovery_sms_last_attempt_at": datetime.now(timezone.utc).isoformat(),
                        "recovery_sms_sent_at": None,
                    }
                ).eq("call_id", call_id).execute()
            )

    except Exception as err:
        logger.error("[agent] Recovery SMS pipeline failed: %s", str(err))
        # Write error state for cron retry pickup
        try:
            await asyncio.to_thread(
                lambda: supabase.table("calls").update(
                    {
                        "recovery_sms_status": "retrying",
                        "recovery_sms_retry_count": 1,
                        "recovery_sms_last_error": f"AGENT_ERROR: {str(err)}",
                        "recovery_sms_last_attempt_at": datetime.now(timezone.utc).isoformat(),
                    }
                ).eq("call_id", call_id).execute()
            )
        except Exception:
            pass  # last-resort swallow


def create_book_appointment_tool(deps: dict):
    @function_tool(
        name="book_appointment",
        description=(
            "Book a confirmed appointment slot. "
            "Always tell the caller you're booking before calling this tool. "
            "Only use after: "
            "(1) collecting caller name, street name, unit/apartment number, and postal/zip code, "
            "(2) reading the full address back and receiving verbal confirmation, "
            "(3) the caller has selected a slot from the availability results. "
            "Pass unit_number as empty string only if the caller explicitly confirmed there is no unit. "
            "Do NOT ask the caller about urgency -- infer it from the conversation."
        ),
    )
    async def book_appointment(
        context: RunContext,
        slot_start: str,
        slot_end: str,
        street_name: str,
        postal_code: str,
        caller_name: str,
        unit_number: str = "",
        urgency: str = "routine",
    ) -> str:
        tenant_id = deps.get("tenant_id")
        supabase = deps["supabase"]

        # Combine street_name + unit_number + postal_code into service_address
        parts = [p for p in [street_name, unit_number, postal_code] if p]
        service_address = ", ".join(parts) if parts else "Address to be confirmed"

        if not slot_start or not slot_end:
            return "I need a bit more information to complete the booking. Could you confirm the time you would like?"

        if not tenant_id:
            return "I was unable to confirm the booking. Please call back and we will try again."

        # Idempotency guard: if this exact slot was already successfully booked earlier
        # in this call, return the cached confirmation without re-running the booking.
        # Prevents duplicate side effects (recovery SMS, calendar events) when Gemini
        # invokes the tool twice for the same slot in quick succession.
        _slot_key = f"{slot_start}|{slot_end}"
        cached_response = deps.get("_last_booked_slot_response")
        if cached_response and deps.get("_last_booked_slot_key") == _slot_key:
            logger.info(
                "[agent] book_appointment: idempotent re-invocation for call=%s slot=%s",
                deps.get("call_id"),
                _slot_key,
            )
            return cached_response

        # Fetch tenant timezone and config
        tenant_result = await asyncio.to_thread(
            lambda: supabase.table("tenants")
            .select("tenant_timezone, working_hours, slot_duration_mins, business_name, default_locale")
            .eq("id", tenant_id)
            .single()
            .execute()
        )
        tenant = tenant_result.data if tenant_result.data else None
        tenant_timezone = (tenant.get("tenant_timezone") if tenant else None) or "America/Chicago"

        # Attempt atomic slot booking
        try:
            result = await atomic_book_slot(
                supabase,
                tenant_id=tenant_id,
                call_id=deps.get("call_uuid") or None,
                start_time=slot_start,
                end_time=slot_end,
                address=service_address,
                caller_name=caller_name or "Caller",
                caller_phone=deps.get("from_number", ""),
                urgency=urgency or "routine",
                zone_id=None,
                postal_code=postal_code or None,
                street_name=street_name or None,
            )
        except Exception as booking_err:
            logger.error("[agent] atomic_book_slot error: %s", str(booking_err))
            return "I was unable to confirm the booking right now. Let me take your information and someone will call you back to schedule."

        if not result.get("success"):
            # Late duplicate guard: if a prior successful booking of THIS EXACT slot
            # has already cached a response on this call, this is a concurrent duplicate
            # invocation arriving AFTER the first booking committed. Return the cached
            # success response instead of treating it as a real slot_taken (which would
            # fire a spurious recovery SMS for an already-booked slot).
            # Key match is required so that a legitimate attempt at a *different* slot
            # after a prior success doesn't accidentally return the old confirmation.
            if deps.get("_last_booked_slot_key") == _slot_key:
                logger.info(
                    "[agent] book_appointment: slot_taken after prior success for same slot; returning cached response for call=%s",
                    deps.get("call_id"),
                )
                return deps.get(
                    "_last_booked_slot_response",
                    "Your appointment is already confirmed. Is there anything else I can help you with?",
                )

            # Slot was taken -- recalculate next available
            now_iso = datetime.now(timezone.utc).isoformat()

            current_bookings, current_events, current_zones, current_buffers = await asyncio.gather(
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

            end_date_str = to_local_date_string(slot_end, tenant_timezone)
            next_slots = calculate_available_slots(
                working_hours=tenant.get("working_hours") or {} if tenant else {},
                slot_duration_mins=(tenant.get("slot_duration_mins") if tenant else None) or 60,
                existing_bookings=current_bookings.data or [],
                external_blocks=current_events.data or [],
                zones=current_zones.data or [],
                zone_pair_buffers=format_zone_pair_buffers(current_buffers.data or []),
                target_date=end_date_str,
                tenant_timezone=tenant_timezone,
                max_slots=1,
            )

            if len(next_slots) > 0:
                next_slot_text = format_slot_for_speech(next_slots[0]["start"], tenant_timezone)
            else:
                next_slot_text = "tomorrow morning"

            # Write booking_outcome: 'attempted'
            await asyncio.to_thread(
                lambda: supabase.table("calls").update(
                    {"booking_outcome": "attempted"}
                ).eq("call_id", deps.get("call_id", "")).is_("booking_outcome", "null").execute()
            )

            # Send recovery SMS (non-blocking) — fire at most ONCE per call, even if
            # multiple slot_taken events occur across different slots. The recovery SMS
            # is a generic "couldn't book you" message, not slot-specific, so one per
            # call is the correct semantic. The check + set is synchronous (no await
            # between them), so it's race-safe on the single-threaded event loop.
            if not deps.get("_recovery_sms_fired"):
                deps["_recovery_sms_fired"] = True
                asyncio.create_task(
                    _send_recovery_sms(deps, tenant, urgency, caller_name)
                )

            return f"That slot was just taken. The next available time is {next_slot_text}. Would you like me to book that instead?"

        # Success — compute and cache the confirmation response SYNCHRONOUSLY before
        # any await. A concurrent duplicate invocation (that lost the race to
        # atomic_book_slot) will see this cache via the late-guard above and return
        # the success response instead of firing a spurious recovery SMS. Previously
        # the cache was set AFTER two awaited DB updates, opening a ~100-200ms window
        # where a duplicate could fall through to the slot_taken branch.
        appointment_id = result.get("appointment_id")

        formatted_time = format_slot_for_speech(slot_start, tenant_timezone)
        return_msg = (
            f"Your appointment is confirmed for {formatted_time}. "
            "You will receive a confirmation. Is there anything else I can help you with?"
        )

        deps["_last_booked_slot_key"] = _slot_key
        deps["_last_booked_slot_response"] = return_msg

        # Now safe to do the awaited follow-up work. Write booking_outcome immediately
        # so it persists even if the caller hangs up during calendar push or SMS.
        await asyncio.to_thread(
            lambda: supabase.table("calls").update(
                {"booking_outcome": "booked"}
            ).eq("call_id", deps.get("call_id", "")).execute()
        )

        # Backfill appointment call_id if it was NULL at booking time
        # (call_uuid may not have been populated yet from the background DB task)
        if appointment_id and deps.get("call_uuid"):
            try:
                await asyncio.to_thread(
                    lambda: supabase.table("appointments")
                    .update({"call_id": deps["call_uuid"]})
                    .eq("id", appointment_id)
                    .is_("call_id", "null")
                    .execute()
                )
            except Exception:
                pass  # non-critical — post-call pipeline has fallback

        # Calendar sync — truly fire-and-forget so the tool returns quickly. A slow tool
        # (awaited side effects) caused the AI to go silent, which let the caller's speech
        # trigger duplicate invocations and a spurious recovery SMS.
        if appointment_id:
            async def _push_calendar_bg():
                try:
                    await asyncio.to_thread(
                        lambda: push_booking_to_calendar(tenant_id, appointment_id)
                    )
                except Exception as cal_err:
                    logger.error("[agent] Calendar push failed: %s", str(cal_err))
            asyncio.create_task(_push_calendar_bg())

        # Caller SMS confirmation — truly fire-and-forget for the same reason.
        sms_locale = (tenant.get("default_locale") if tenant else None) or "en"
        async def _send_confirmation_sms_bg():
            try:
                await asyncio.to_thread(
                    lambda: send_caller_sms(
                        to=deps.get("from_number"),
                        from_number=deps.get("to_number"),
                        business_name=(tenant.get("business_name") if tenant else None) or "Your service provider",
                        date=_format_date_for_sms(slot_start, tenant_timezone),
                        time=_format_time_for_sms(slot_start, tenant_timezone),
                        address=service_address or "",
                        locale=sms_locale,
                    )
                )
            except Exception as sms_err:
                logger.error("[agent] Caller SMS failed: %s", str(sms_err))
        asyncio.create_task(_send_confirmation_sms_bg())

        return return_msg

    return book_appointment
