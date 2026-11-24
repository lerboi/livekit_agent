"""find_appointment, reschedule_appointment, cancel_appointment.

``find_appointment`` returns the caller's upcoming appointments, each with an
``appointment_token``. The other two accept only such a token, so the model
cannot move or cancel an appointment it did not first look up, and the lookup
itself is limited to appointments this call may touch (``store.upcoming``).

Both writes update Voco's row first (a move is a single UPDATE the database
refuses if the new time is held) and the calendar event second.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher

from livekit.agents import RunContext, function_tool

from ....lib.demo import publish_event
from ....lib.tool_filler import tool_filler
from ....tools import _availability_lib as lib
from .. import calendar_sync, store
from .common import (
    LOOKUP_FILLER_DELAY_S,
    TRY_AGAIN,
    WRITE_FILLER_DELAY_S,
    mark_write,
    register_appointment,
    resolve_appointment,
    resolve_slot,
    say_time,
    slot_first_offered_at,
    state,
    tz_of,
    unconfirmed,
)

logger = logging.getLogger("voco-agent")

MAX_MATCHES = 3
NAME_MATCH_RATIO = 0.72

_FIND = {
    "name": "find_appointment",
    "description": (
        "Look up the caller's upcoming appointment. Call it when the caller "
        "wants to move, cancel or check an existing appointment, as soon as you "
        "have their name. Returns the appointments found, each with an "
        "appointment_token. Call it directly, without announcing it. The return "
        "is a state+directive string; do not read it aloud."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "caller_name": {
                "type": "string",
                "description": "The name the appointment was booked under.",
            },
        },
        "required": ["caller_name"],
    },
}

_RESCHEDULE = {
    "name": "reschedule_appointment",
    "description": (
        "Move an existing appointment to a new time the caller has agreed to. "
        "Pass the appointment_token from find_appointment and the slot_token of "
        "the new time from check_slot or check_day; never invent either. Call "
        "it directly, without announcing it. Do not say the appointment has "
        "been moved before this tool returns RESCHEDULED. The return is a "
        "state+directive string; do not read it aloud."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "appointment_token": {
                "type": "string",
                "description": "The appointment_token returned by find_appointment.",
            },
            "slot_token": {
                "type": "string",
                "description": "The slot_token returned by check_slot or check_day for the new time.",
            },
        },
        "required": ["appointment_token", "slot_token"],
    },
}

_CANCEL = {
    "name": "cancel_appointment",
    "description": (
        "Cancel an existing appointment after the caller has clearly said they "
        "want it cancelled. Pass the appointment_token from find_appointment; "
        "never invent one. Call it directly, without announcing it. Do not say "
        "it is cancelled before this tool returns CANCELLED. The return is a "
        "state+directive string; do not read it aloud."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "appointment_token": {
                "type": "string",
                "description": "The appointment_token returned by find_appointment.",
            },
        },
        "required": ["appointment_token"],
    },
}


# ── name matching ──────────────────────────────────────────────────────────

def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).strip()


def name_matches(spoken: str, stored: str) -> bool:
    """Whether a name heard on the call is the name on the appointment.

    Speech-to-text spells names loosely ("Alex Tan" / "Alex Tann"), and callers
    give a first name where the booking has both, so this accepts a close
    spelling of the whole name or of any shared word.
    """
    a, b = _norm(spoken), _norm(stored)
    if not a or not b or b == "caller":
        return False
    if a == b or SequenceMatcher(None, a, b).ratio() >= NAME_MATCH_RATIO:
        return True
    words_a, words_b = a.split(), b.split()
    shorter, longer = (words_a, words_b) if len(words_a) <= len(words_b) else (words_b, words_a)
    return all(
        any(SequenceMatcher(None, w, other).ratio() >= 0.8 for other in longer)
        for w in shorter
    )


def _describe(deps: dict, appointment: dict, index: int) -> str:
    token = register_appointment(deps, appointment)
    reason = (appointment.get("notes") or "").strip()
    name = (appointment.get("caller_name") or "").strip()
    return (
        f"{index}.{say_time(appointment['start_time'], tz_of(deps))}"
        f"{f' for=[{reason}]' if reason else ''}"
        f"{f' name=[{name}]' if name else ''}"
        f" token={token}"
    )


# ── find ───────────────────────────────────────────────────────────────────

async def find_appointment_impl(deps: dict, caller_name: str) -> str:
    if not deps.get("tenant_id"):
        return state(deps, f"STATE:lookup_failed reason=no_business | DIRECTIVE:{TRY_AGAIN}.")
    rows = await store.upcoming(deps)

    # Appointments made earlier on this same call are the caller's own: they
    # need no name check, and they are included even before the database row
    # has been linked to the call.
    own = [
        entry["appointment"]
        for entry in (deps.get("_appt_tokens") or {}).values()
        if entry.get("own") and entry["appointment"].get("status") == "confirmed"
    ]
    own_ids = {a["id"] for a in own}
    rows = own + [r for r in rows if r["id"] not in own_ids]
    named = [r for r in rows if r["id"] in own_ids or name_matches(caller_name, r.get("caller_name"))]
    if deps.get("is_test_call"):
        matches = named
    else:
        # Production scope is already "booked from this phone number". A name,
        # when given, narrows it; it never widens it.
        matches = named or rows

    # An appointment booked earlier on this same call is the one a caller means
    # by "move it" or "cancel it". Older appointments under the same name are
    # counted, not listed: on a production call on 2026-11-25 the agent had
    # just booked Tuesday at nine, was asked to "move it", found a second
    # appointment under the name and asked the caller which one they meant.
    others = 0
    if own:
        others = len([m for m in matches if m["id"] not in own_ids])
        matches = [m for m in matches if m["id"] in own_ids]

    lib.log_tool_call(deps, {"name": "find_appointment", "success": True, "matches": len(matches)})
    if not matches:
        return state(
            deps,
            "STATE:appointment_not_found"
            " | DIRECTIVE:say you cannot find an upcoming appointment under that"
            " name. Ask them to say or spell the name once more and look again."
            " If it is still not found, offer to book a new appointment.",
        )
    matches = matches[:MAX_MATCHES]
    listed = "; ".join(_describe(deps, row, i) for i, row in enumerate(matches, 1))
    if len(matches) == 1:
        # The first clause matters: told only to "tell the caller what you
        # found", gpt-4.1-mini described the appointment, then checked the new
        # time and spoke again: two replies and two confirmations for one move
        # (scripted calls, 2026-11-24).
        directive = (
            "if the caller already named the new time they want, say nothing"
            " yet: call check_slot for it now, then ask once whether to move"
            " the appointment there. Otherwise tell the caller the day and time"
            " you found; if they have not said what they want to do with it,"
            " ask. To move it, get the new time with check_slot or check_day"
            " first."
        )
    else:
        directive = "tell the caller the appointments you found and ask which one they mean."
    note = ""
    if others:
        note = (
            f" booked_on_this_call=true other_upcoming={others}"
        )
        directive = (
            "this is the appointment booked earlier on this call, which is the"
            " one the caller means. Do not mention their other appointments"
            " unless they say they mean a different one. " + directive
        )
    return state(
        deps,
        f"STATE:appointment_found count={len(matches)}{note} | APPOINTMENTS: {listed} | DIRECTIVE:{directive}",
    )


# ── reschedule ─────────────────────────────────────────────────────────────

async def reschedule_appointment_impl(deps: dict, raw_arguments: dict) -> str:
    tz = tz_of(deps)
    entry = resolve_appointment(deps, (raw_arguments.get("appointment_token") or "").strip())
    if entry is None:
        return state(
            deps,
            "STATE:reschedule_invalid reason=unknown_appointment"
            " | DIRECTIVE:nothing has changed. Call find_appointment with the"
            " caller's name first.",
        )
    appointment = entry["appointment"]
    slot = resolve_slot(deps, (raw_arguments.get("slot_token") or "").strip())
    if slot is None:
        return state(
            deps,
            "STATE:reschedule_invalid reason=unknown_slot"
            " | DIRECTIVE:nothing has changed. Call check_slot for the new time"
            " the caller wants, then reschedule with the token it returns.",
        )
    new_start, new_end = slot["slot_start_utc"], slot["slot_end_utc"]
    old_start = appointment["start_time"]
    new_when, old_when = say_time(new_start, tz), say_time(old_start, tz)

    if _same_instant(old_start, new_start):
        return state(
            deps,
            f"STATE:already_at_that_time time=[{new_when}]"
            " | DIRECTIVE:nothing has changed. Tell the caller the appointment is"
            " already at that time and ask if they want a different one.",
        )

    if unconfirmed(
        deps, f"move:{appointment['id']}:{new_start}", slot_first_offered_at(deps, new_start)
    ):
        return state(
            deps,
            f"STATE:needs_confirmation from=[{old_when}] to=[{new_when}]"
            " | DIRECTIVE:nothing has changed yet. The caller has not agreed to the"
            " new time. Say the new day and time back once and ask if you should"
            " move it. When they say yes, call reschedule_appointment again with"
            " the same tokens.",
        )

    try:
        outcome = await store.move(deps, appointment, start_utc=new_start, end_utc=new_end)
    except Exception as err:  # noqa: BLE001
        logger.error("[appointments] reschedule failed: %s", err)
        return state(deps, f"STATE:reschedule_failed reason=error | DIRECTIVE:nothing has changed; {TRY_AGAIN}.")

    if outcome == "taken":
        deps.pop("_slot_cache", None)
        return state(
            deps,
            f"STATE:slot_taken kept=[{old_when}]"
            " | DIRECTIVE:nothing has changed; the appointment is still at its"
            " original time. Say the new time was just taken and offer to look"
            " for another (call check_day).",
        )
    if outcome == "gone":
        return state(
            deps,
            "STATE:reschedule_failed reason=appointment_no_longer_active"
            " | DIRECTIVE:say that appointment is no longer on the calendar and"
            " offer to book a new one.",
        )

    appointment["start_time"], appointment["end_time"] = new_start, new_end
    mark_write(deps)
    lib.log_tool_call(deps, {
        "name": "reschedule_appointment", "success": True, "appointment_id": appointment["id"],
        "from": old_start, "slot_start": new_start, "slot_end": new_end,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    publish_event(
        deps, "rescheduled",
        appointment_id=appointment["id"], start=new_start, end=new_end, when=new_when,
        previous_start=old_start, previous_when=old_when,
        caller_name=appointment.get("caller_name"), reason=appointment.get("notes"),
    )
    calendar = await calendar_sync.move(deps, appointment)
    logger.info(
        "[appointments] rescheduled appointment=%s calendar=%s call=%s",
        appointment["id"], calendar, deps.get("call_id"),
    )
    return state(
        deps,
        f"RESCHEDULED: confirm the new day and time [{new_when}] in ONE short"
        " sentence, then ask if there is anything else",
    )


def _same_instant(a: str, b: str) -> bool:
    def parse(value: str) -> datetime:
        return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    try:
        return parse(a) == parse(b)
    except ValueError:
        return a == b


# ── cancel ─────────────────────────────────────────────────────────────────

async def cancel_appointment_impl(deps: dict, raw_arguments: dict) -> str:
    tz = tz_of(deps)
    entry = resolve_appointment(deps, (raw_arguments.get("appointment_token") or "").strip())
    if entry is None:
        return state(
            deps,
            "STATE:cancel_invalid reason=unknown_appointment"
            " | DIRECTIVE:nothing has changed. Call find_appointment with the"
            " caller's name first.",
        )
    appointment = entry["appointment"]
    when = say_time(appointment["start_time"], tz)

    if unconfirmed(deps, f"cancel:{appointment['id']}", entry.get("created_at", 0)):
        return state(
            deps,
            f"STATE:needs_confirmation time=[{when}]"
            " | DIRECTIVE:nothing has changed yet. Say the day and time of the"
            " appointment and ask the caller to confirm they want it cancelled."
            " When they say yes, call cancel_appointment again with the same token.",
        )

    try:
        cancelled = await store.cancel(deps, appointment)
    except Exception as err:  # noqa: BLE001
        logger.error("[appointments] cancel failed: %s", err)
        return state(deps, f"STATE:cancel_failed reason=error | DIRECTIVE:nothing has changed; {TRY_AGAIN}.")
    if not cancelled:
        return state(
            deps,
            "STATE:already_cancelled"
            " | DIRECTIVE:tell the caller that appointment is no longer on the"
            " calendar and ask if there is anything else.",
        )

    appointment["status"] = "cancelled"
    mark_write(deps)
    if deps.get("_booked_appointment_id") == appointment["id"]:
        # Booked and cancelled on the same call: post-call must not record a booking.
        deps["_booking_succeeded"] = False
        deps.pop("_last_booked_slot_key", None)
        deps.pop("_last_booked_slot_response", None)
    for token, item in list((deps.get("_appt_tokens") or {}).items()):
        if item["appointment"]["id"] == appointment["id"]:
            deps["_appt_tokens"].pop(token, None)
    lib.log_tool_call(deps, {
        "name": "cancel_appointment", "success": True, "appointment_id": appointment["id"],
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    publish_event(
        deps, "cancelled",
        appointment_id=appointment["id"], start=appointment["start_time"],
        end=appointment["end_time"], when=when,
        caller_name=appointment.get("caller_name"), reason=appointment.get("notes"),
    )
    calendar = await calendar_sync.remove(deps, appointment)
    logger.info(
        "[appointments] cancelled appointment=%s calendar=%s call=%s",
        appointment["id"], calendar, deps.get("call_id"),
    )
    return state(
        deps,
        f"CANCELLED: tell the caller the appointment on [{when}] is cancelled in"
        " ONE short sentence, then ask if there is anything else",
    )


# ── tool factories ─────────────────────────────────────────────────────────

def create_find_appointment_tool(deps: dict):
    @function_tool(raw_schema=_FIND)
    async def find_appointment(raw_arguments: dict, context: RunContext) -> str:
        name = (raw_arguments.get("caller_name") or "").strip()
        try:
            async with tool_filler(context, deps, "generic", delay=LOOKUP_FILLER_DELAY_S):
                return await find_appointment_impl(deps, name)
        except Exception as err:  # noqa: BLE001
            logger.error("[appointments] find_appointment failed: %r", err)
            return state(deps, f"STATE:lookup_failed | DIRECTIVE:{TRY_AGAIN}.")

    return find_appointment


def create_reschedule_appointment_tool(deps: dict):
    @function_tool(raw_schema=_RESCHEDULE)
    async def reschedule_appointment(raw_arguments: dict, context: RunContext) -> str:
        async with tool_filler(
            context, deps, "generic", interval=6.0, max_steps=2, delay=WRITE_FILLER_DELAY_S,
        ):
            return await reschedule_appointment_impl(deps, raw_arguments)

    return reschedule_appointment


def create_cancel_appointment_tool(deps: dict):
    @function_tool(raw_schema=_CANCEL)
    async def cancel_appointment(raw_arguments: dict, context: RunContext) -> str:
        async with tool_filler(
            context, deps, "generic", interval=6.0, max_steps=2, delay=WRITE_FILLER_DELAY_S,
        ):
            return await cancel_appointment_impl(deps, raw_arguments)

    return cancel_appointment
