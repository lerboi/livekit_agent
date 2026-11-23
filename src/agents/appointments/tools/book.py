"""book_appointment: take the slot, then put it on the calendar.

Order of work, and why:

1. Resolve the slot_token. The model never passes a date or a time here.
2. Confirm gate: refuse, once, a time the caller has not had a turn to accept.
3. ``book_appointment_atomic``: the slot is now held, or it was taken.
4. Follow-up writes and the calendar event run together. The calendar write
   has a short budget; the return says whether it was saved, is still going,
   or failed. The caller is told "booked" in every case, because the
   appointment exists.

Test calls never write to an external calendar and never send SMS; a Demo
Studio call writes to the one calendar its server-set metadata names
(``calendars.calendar_target``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from livekit.agents import RunContext, function_tool

from ....lib.demo import publish_event
from ....lib.tool_filler import tool_filler
from ....tools import _availability_lib as lib
from .. import calendar_sync, store
from .availability import _context, next_opening, open_slots
from .common import (
    TRY_AGAIN,
    WRITE_FILLER_DELAY_S,
    mark_write,
    register_appointment,
    resolve_slot,
    say_time,
    slot_first_offered_at,
    state,
    tz_of,
    unconfirmed,
)

logger = logging.getLogger("voco-agent")

_SCHEMA = {
    "name": "book_appointment",
    "description": (
        "Book a new appointment after the caller has agreed to the time. Pass "
        "the slot_token for that time exactly as check_slot or check_day "
        "returned it; never invent one. Call it directly, without announcing "
        "it. Do not say the appointment is booked before this tool returns "
        "BOOKED. The return is a state+directive string; do not read it aloud."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slot_token": {
                "type": "string",
                "description": "The slot_token returned by check_slot or check_day for the chosen time.",
            },
            "caller_name": {
                "type": "string",
                "description": "The caller's name. Empty string only if they declined to give it.",
            },
            "reason": {
                "type": "string",
                "description": "What the appointment is for, in a few words, as the caller described it.",
            },
        },
        "required": ["slot_token", "caller_name"],
    },
}


async def _next_after_taken(deps: dict, slot_start: str) -> str:
    """' next_available=<speech> token=<token>' for the first time still open,
    looked up fresh, or '' when nothing is."""
    deps.pop("_slot_cache", None)
    ctx = await _context(deps)
    if isinstance(ctx, str):
        return ""
    tenant, tz, sched = ctx
    date = lib.to_local_date_string(slot_start, tz)
    taken_at = lib._slot_start_dt_utc({"start": slot_start})
    same_day = [
        s for s in open_slots(tenant, date, sched, tz) if lib._slot_start_dt_utc(s) > taken_at
    ]
    if same_day:
        token = lib.register_slot_token(deps, same_day[0]["start"], same_day[0]["end"])
        deps["_last_offered_token"] = token
        return f" next_available={say_time(same_day[0]['start'], tz)} token={token}"
    later = next_opening(deps, tenant, sched, tz, date)
    return f" next_available={later[0]} token={later[1]}" if later else ""


async def book_appointment_impl(deps: dict, raw_arguments: dict) -> str:
    slot = resolve_slot(deps, (raw_arguments.get("slot_token") or "").strip())
    caller_name = (raw_arguments.get("caller_name") or "").strip()
    reason = (raw_arguments.get("reason") or "").strip()
    tz = tz_of(deps)

    if slot is None:
        return state(
            deps,
            "STATE:booking_invalid reason=unknown_slot"
            " | DIRECTIVE:nothing is booked. Call check_slot for the time the"
            " caller wants, then book with the token it returns.",
        )
    slot_start, slot_end = slot["slot_start_utc"], slot["slot_end_utc"]

    if unconfirmed(deps, f"book:{slot_start}", slot_first_offered_at(deps, slot_start)):
        return state(
            deps,
            f"STATE:needs_confirmation time=[{say_time(slot_start, tz)}]"
            " | DIRECTIVE:nothing is booked yet. The caller has not agreed to this"
            " time. Say the day and time back once and ask if you should book it."
            " When they say yes, call book_appointment again with the same slot_token.",
        )

    if not deps.get("tenant_id"):
        return state(deps, f"STATE:booking_failed reason=no_business | DIRECTIVE:nothing is booked; {TRY_AGAIN}.")

    # The same slot booked twice on one call returns the first answer.
    slot_key = f"{slot_start}|{slot_end}"
    if deps.get("_last_booked_slot_key") == slot_key and deps.get("_last_booked_slot_response"):
        return state(deps, deps["_last_booked_slot_response"])

    try:
        result = await store.book(deps, start_utc=slot_start, end_utc=slot_end, caller_name=caller_name)
    except Exception as err:  # noqa: BLE001
        logger.error("[appointments] booking failed: %s", err)
        return state(deps, f"STATE:booking_failed reason=error | DIRECTIVE:nothing is booked; {TRY_AGAIN}.")

    if not result.get("success"):
        if deps.get("_last_booked_slot_key") == slot_key and deps.get("_last_booked_slot_response"):
            return state(deps, deps["_last_booked_slot_response"])
        lib.log_tool_call(deps, {
            "name": "book_appointment", "success": False, "reason": "slot_taken",
            "slot_start": slot_start, "slot_end": slot_end,
        })
        alternative = await _next_after_taken(deps, slot_start)
        return state(
            deps,
            f"STATE:slot_taken{alternative}"
            " | DIRECTIVE:nothing is booked. Say that time was just taken"
            + (" and offer the next available time above." if alternative else " and ask for another time."),
        )

    appointment_id = result.get("appointment_id")
    when = say_time(slot_start, tz)
    appointment = {
        "id": appointment_id,
        "start_time": slot_start,
        "end_time": slot_end,
        "caller_name": caller_name or "Caller",
        "caller_phone": deps.get("from_number") or "",
        "notes": reason or None,
        "status": "confirmed",
        "call_id": deps.get("call_uuid"),
        "external_event_id": None,
        "external_event_provider": None,
    }

    # Set before the first await: a duplicate call for the same slot that
    # loses the race reads these and returns the same confirmation.
    response = (
        f"BOOKED: confirm the day and time [{when}] in ONE short sentence,"
        " then ask if there is anything else"
    )
    deps["_last_booked_slot_key"] = slot_key
    deps["_last_booked_slot_response"] = response
    deps["_booking_succeeded"] = True
    deps["_booked_appointment_id"] = appointment_id
    deps["_booked_caller_name"] = caller_name or None
    # Register before mark_write: the claim guard compares the newest token
    # with the last write, and "you're booked" is now true.
    register_appointment(deps, appointment, own=True)
    mark_write(deps)
    lib.log_tool_call(deps, {
        "name": "book_appointment", "success": True, "appointment_id": appointment_id,
        "slot_start": slot_start, "slot_end": slot_end,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    publish_event(
        deps, "booked",
        appointment_id=appointment_id, start=slot_start, end=slot_end, when=when,
        caller_name=caller_name or None, reason=reason or None,
    )

    _, calendar = await asyncio.gather(
        store.save_details(deps, appointment_id, reason=reason),
        calendar_sync.create(deps, appointment),
    )
    register_appointment(deps, appointment, own=True)  # now carries the calendar event id
    logger.info(
        "[appointments] booked appointment=%s calendar=%s call=%s",
        appointment_id, calendar, deps.get("call_id"),
    )
    return state(deps, response)


def create_book_appointment_tool(deps: dict):
    @function_tool(raw_schema=_SCHEMA)
    async def book_appointment(raw_arguments: dict, context: RunContext) -> str:
        async with tool_filler(
            context, deps, "booking", interval=6.0, max_steps=2, delay=WRITE_FILLER_DELAY_S,
        ):
            return await book_appointment_impl(deps, raw_arguments)

    return book_appointment
