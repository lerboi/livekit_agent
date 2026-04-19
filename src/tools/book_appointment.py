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


# DB constraint `appointments_urgency_check` only accepts these three values.
# Gemini has been observed passing freeform strings (e.g. "high"), which causes
# atomic_book_slot to fail with a CHECK constraint violation (backlog 999.1).
_ALLOWED_URGENCY = {"emergency", "urgent", "routine"}
_URGENCY_ALIASES = {
    "high": "urgent",
    "medium": "urgent",
    "normal": "routine",
    "low": "routine",
    "standard": "routine",
    "critical": "emergency",
    "immediate": "emergency",
    "asap": "emergency",
}


def _normalize_urgency(value: str | None) -> str:
    if not value:
        return "routine"
    v = value.strip().lower()
    if v in _ALLOWED_URGENCY:
        return v
    return _URGENCY_ALIASES.get(v, "routine")


def _ensure_utc_iso(iso_str: str) -> str:
    """
    Canonicalize any ISO string Gemini may emit to a UTC-offset form.

    Gemini occasionally strips the '+00:00' offset when re-emitting the
    slot_start / slot_end values from check_availability's STATE line
    (especially since Phase 60 added a human-readable `speech=` field
    alongside the UTC ISO). A naive datetime flowing into .astimezone()
    is treated as system-local time — on Railway that is UTC, on other
    hosts it could be anything — which silently shifts the wall-clock
    time seen by SMS/calendar/RPC consumers.

    Contract: check_availability returns slot_start/slot_end as UTC ISO.
    If Gemini drops the offset, we re-attach UTC here.
    """
    if iso_str.endswith("Z"):
        iso_str = iso_str[:-1] + "+00:00"
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _format_date_for_sms(iso_str: str, tenant_timezone: str) -> str:
    """Format ISO datetime to 'Tuesday, March 4th' for SMS."""
    iso_str = _ensure_utc_iso(iso_str)
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
    iso_str = _ensure_utc_iso(iso_str)
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
            "Book an appointment into the caller's selected slot. "
            "CRITICAL PRECONDITION: before calling this tool, you must have read back the caller's "
            "name (if captured) and full service address in one utterance, and the caller must have "
            "acknowledged or silently accepted the readback (see the BEFORE BOOKING — READBACK rule "
            "in the system prompt). Do not call this tool until the readback is complete. "
            "This tool's return is a state+directive string — do not read it aloud. "
            "DO NOT speak the words 'confirmed', 'booked', 'all set', 'see you tomorrow/at...', "
            "or any specific appointment time as a settled fact before invoking this tool. "
            "Always speak a short filler phrase first ('Let me get that booked in for you'), "
            "then immediately invoke this tool in the same turn. "
            "Pass unit_number as empty string only if the caller explicitly confirmed there is no unit. "
            "Do NOT ask the caller about urgency -- infer it from the conversation. "
            "urgency MUST be exactly one of: 'emergency', 'urgent', 'routine'. "
            "Never pass any other value (e.g. 'high', 'low', 'medium'); default to 'routine' if unsure."
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
            return (
                "STATE:booking_invalid reason=missing_slot_fields"
                " | DIRECTIVE:apologize briefly; ask the caller to confirm the time they would like;"
                " call book_appointment again once complete."
            )

        # Canonicalize slot_start / slot_end to UTC ISO up front. Gemini may
        # drop the '+00:00' offset (especially after Phase 60 added a
        # human-readable `speech=` field to check_availability's STATE line),
        # which silently shifts the wall-clock seen by SMS / calendar / RPC
        # consumers. Fix it once here so every downstream caller sees UTC.
        try:
            slot_start = _ensure_utc_iso(slot_start)
            slot_end = _ensure_utc_iso(slot_end)
        except ValueError:
            return (
                "STATE:booking_invalid reason=malformed_slot_iso"
                " | DIRECTIVE:apologize briefly; call check_availability again for the"
                " same date and time to get a fresh slot, then call book_appointment"
                " with the exact start/end values from the fresh result."
            )

        if not tenant_id:
            return (
                "STATE:booking_failed reason=no_tenant_id"
                " | DIRECTIVE:apologize; offer to transfer to a human or take a callback via"
                " capture_lead; do not attempt to book again in this call. Do not repeat this"
                " message text on-air."
            )

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

        # Normalize urgency to a DB-constraint-valid value. Gemini has been observed
        # passing freeform strings like "high" which violate appointments_urgency_check
        # (backlog 999.1). Defense-in-depth alongside the tool-description enumeration.
        normalized_urgency = _normalize_urgency(urgency)
        if normalized_urgency != (urgency or "routine"):
            logger.info(
                "[agent] book_appointment: normalized urgency %r -> %r for call=%s",
                urgency,
                normalized_urgency,
                deps.get("call_id"),
            )

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
                urgency=normalized_urgency,
                zone_id=None,
                postal_code=postal_code or None,
                street_name=street_name or None,
            )
        except Exception as booking_err:
            logger.error("[agent] atomic_book_slot error: %s", str(booking_err))
            return (
                "STATE:booking_failed reason=rpc_error"
                " | DIRECTIVE:apologize; offer to transfer to a human or take a callback via"
                " capture_lead; do not attempt to book again in this call. Do not repeat this"
                " message text on-air."
            )

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
                    "STATE:booking_succeeded reason=idempotent_duplicate"
                    " | DIRECTIVE:confirm verbally to the caller using the name and address you already"
                    " read back; do not restate the time; ask if there is anything else before wrapping"
                    " up.",
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
                    _send_recovery_sms(deps, tenant, normalized_urgency, caller_name)
                )

            deps.setdefault("_tool_call_log", []).append({
                "name": "book_appointment",
                "success": False,
                "reason": "slot_taken",
                "slot_start": slot_start,
                "slot_end": slot_end,
                "ts": datetime.now(timezone.utc).isoformat(),
            })

            return (
                "STATE:slot_taken"
                f" next_available={next_slot_text}"
                " | DIRECTIVE:tell the caller that slot was just booked by someone else; offer"
                " the next available time listed above as an alternative and ask if they want"
                " to book it."
            )

        # Success — compute and cache the confirmation response SYNCHRONOUSLY before
        # any await. A concurrent duplicate invocation (that lost the race to
        # atomic_book_slot) will see this cache via the late-guard above and return
        # the success response instead of firing a spurious recovery SMS. Previously
        # the cache was set AFTER two awaited DB updates, opening a ~100-200ms window
        # where a duplicate could fall through to the slot_taken branch.
        appointment_id = result.get("appointment_id")

        formatted_time = format_slot_for_speech(slot_start, tenant_timezone)
        return_msg = (
            "STATE:booking_succeeded"
            f" appointment_id={appointment_id}"
            " | DIRECTIVE:confirm verbally to the caller using the name and address you already"
            " read back; do not restate the time (the caller already heard it during the slot"
            " offer); ask if there is anything else before wrapping up. Do not repeat this"
            " message text on-air."
        )

        deps["_last_booked_slot_key"] = _slot_key
        deps["_last_booked_slot_response"] = return_msg

        # Authoritative booking flags for post-call reconciliation. These are set
        # synchronously (no await between) so post-call can correct the DB even if
        # the mid-call update below races the background db_task that creates the
        # calls row.
        deps["_booking_succeeded"] = True
        deps["_booked_appointment_id"] = appointment_id
        deps["_booked_caller_name"] = caller_name or None

        # Audit trail for post-call hallucination detection.
        deps.setdefault("_tool_call_log", []).append({
            "name": "book_appointment",
            "success": True,
            "appointment_id": appointment_id,
            "slot_start": slot_start,
            "slot_end": slot_end,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

        # Now safe to do the awaited follow-up work. Write booking_outcome immediately
        # so it persists even if the caller hangs up during calendar push or SMS.
        # If the calls row hasn't been inserted yet (db_task race), this matches zero
        # rows silently; post-call reconciliation handles that case.
        result_update = await asyncio.to_thread(
            lambda: supabase.table("calls").update(
                {"booking_outcome": "booked"}
            ).eq("call_id", deps.get("call_id", "")).execute()
        )
        if not (result_update.data if result_update else None):
            logger.warning(
                "[booking] mid-call booking_outcome update matched zero rows "
                "(race with db_task); will be reconciled in post-call. call_id=%s",
                deps.get("call_id"),
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
