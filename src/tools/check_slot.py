"""
check_slot -- verify whether a specific (date, time) is bookable.

Replaces the date+time branch of the former monolithic check_availability.
Uses raw_schema so Gemini's serializer enforces `required: [date, time]`
and the HH:MM / YYYY-MM-DD patterns before invocation. That structural
guarantee is what moved Phase 63.1-08/09/10 bug-classes out of prose.

Returns a short STATE+DIRECTIVE string (≤150 chars in the common path) so
Gemini 3.1 Flash Live can begin audio generation with minimal ingestion lag.

2026-06-11 naturalness pass (findings.md P1): the too_soon and day_empty
branches now pair the rejection with the nearest bookable alternative
(earliest-viable-today, else the first opening in the next 2 days), each
carrying a registered slot_token — every "no" arrives with a tool-licensed
"but I could do X" so callers are never sent back to blind guessing.
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timedelta, timezone

from livekit.agents import function_tool, RunContext

from ..utils import format_slot_for_speech
from ._availability_lib import (
    calc_slots_for_dates,
    ensure_tenant,
    fetch_scheduling_data,
    format_date_label,
    log_tool_call,
    parse_hhmm_to_utc,
    register_slot_token,
    tenant_today,
)

logger = logging.getLogger(__name__)


_SCHEMA = {
    "name": "check_slot",
    "description": (
        "Verify whether a specific date and time is bookable. Call this every "
        "time the caller names a concrete hour. Speak a short filler phrase "
        "first ('Let me pull that up real quick'), then invoke in the same turn. "
        "This tool's return is a state+directive string — do not read it aloud."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "pattern": r"^\d{4}-\d{2}-\d{2}$",
                "description": "Target date as YYYY-MM-DD in the business's local timezone.",
            },
            "time": {
                "type": "string",
                "pattern": r"^([01]?\d|2[0-3]):[0-5]\d$",
                "description": "Target time as HH:MM 24-hour in the business's local timezone.",
            },
            "urgency": {
                "type": "string",
                "enum": ["emergency", "urgent", "routine"],
                "description": "Inferred urgency from the conversation. Default 'routine'.",
            },
        },
        "required": ["date", "time"],
    },
}


def create_check_slot_tool(deps: dict):
    @function_tool(raw_schema=_SCHEMA)
    async def check_slot(raw_arguments: dict, context: RunContext) -> str:
        t0 = _time.time()
        call_id = f"cs_{int(t0 * 1000) % 100000}"
        date = (raw_arguments.get("date") or "").strip()
        time_str = (raw_arguments.get("time") or "").strip()
        urgency = raw_arguments.get("urgency") or "routine"

        logger.info(
            "[63.1-DIAG] check_slot ENTRY id=%s date=%r time=%r urgency=%r",
            call_id, date, time_str, urgency,
        )
        try:
            result = await _impl(deps, date, time_str, urgency)
            elapsed_ms = int((_time.time() - t0) * 1000)
            logger.info(
                "[63.1-DIAG] check_slot EXIT id=%s elapsed_ms=%d len=%d preview=%r",
                call_id, elapsed_ms, len(result or ""), (result or "")[:180],
            )
            return result
        except Exception as exc:
            logger.error(
                "[63.1-DIAG] check_slot EXCEPTION id=%s elapsed_ms=%d err=%s",
                call_id, int((_time.time() - t0) * 1000), repr(exc),
            )
            state = "STATE:lookup_failed | DIRECTIVE:apologize briefly; offer capture_lead; do not retry."
            deps["_last_tool_state"] = state
            return state

    return check_slot


def _slot_start_dt(slot: dict) -> datetime:
    """Parse a slot's start ISO (Z- or offset-suffixed) to an aware datetime."""
    s_iso = slot["start"]
    if s_iso.endswith("Z"):
        s_iso = s_iso[:-1] + "+00:00"
    return datetime.fromisoformat(s_iso)


def _find_next_opening(
    deps: dict,
    tenant: dict,
    sched: dict,
    tenant_timezone: str,
    after_date: str,
    days_ahead: int = 2,
) -> tuple[str, str] | None:
    """First open slot in the `days_ahead` days after `after_date`.
    Returns (speech, slot_token) — the token is registered and stashed as
    _last_offered_token — or None if nothing is open. Powers the
    rejection-paired-with-alternative returns (findings.md P1)."""
    try:
        base = datetime.strptime(after_date, "%Y-%m-%d")
    except ValueError:
        return None
    for offset in range(1, days_ahead + 1):
        next_date = (base + timedelta(days=offset)).strftime("%Y-%m-%d")
        next_slots = calc_slots_for_dates(tenant, [next_date], sched, tenant_timezone)
        if next_slots:
            alt = sorted(next_slots, key=lambda s: s.get("start") or "")[0]
            speech = format_slot_for_speech(alt["start"], tenant_timezone)
            token = register_slot_token(deps, alt["start"], alt["end"])
            deps["_last_offered_token"] = token
            return speech, token
    return None


async def _impl(deps: dict, date: str, time_str: str, urgency: str) -> str:
    tenant_id = deps.get("tenant_id")
    if not tenant_id:
        state = "STATE:lookup_failed reason=no_tenant | DIRECTIVE:apologize briefly; offer capture_lead."
        deps["_last_tool_state"] = state
        return state

    if not date or not time_str:
        # Schema makes this unreachable from Gemini, but belt-and-braces.
        state = "STATE:missing_args | DIRECTIVE:ask the caller for a specific date and time."
        deps["_last_tool_state"] = state
        return state

    tenant = await ensure_tenant(deps)
    if not tenant:
        state = "STATE:lookup_failed reason=tenant | DIRECTIVE:apologize briefly; offer capture_lead."
        deps["_last_tool_state"] = state
        return state
    tenant_timezone = tenant.get("tenant_timezone") or "UTC"
    slot_duration = tenant.get("slot_duration_mins") or 60

    today_local = tenant_today(tenant_timezone)
    if date < today_local:
        state = (
            f"STATE:past_date requested={date} today={today_local}"
            " | DIRECTIVE:ask for today or later; do not fabricate times."
        )
        deps["_last_tool_state"] = state
        return state

    requested_utc = parse_hhmm_to_utc(time_str, date, tenant_timezone)
    if requested_utc is None:
        state = "STATE:bad_time_format | DIRECTIVE:ask the caller to restate the time (e.g. '2 PM' or '14:00')."
        deps["_last_tool_state"] = state
        return state

    now_utc = datetime.now(timezone.utc)
    too_soon = requested_utc < now_utc + timedelta(hours=1) and date == today_local

    sched = await fetch_scheduling_data(deps)
    if sched is None:
        state = "STATE:lookup_failed reason=scheduling_data | DIRECTIVE:apologize briefly; offer capture_lead."
        deps["_last_tool_state"] = state
        return state

    all_slots = calc_slots_for_dates(tenant, [date], sched, tenant_timezone)

    # 2026-06-11 naturalness pass (findings.md P1): the too_soon branch used
    # to return BEFORE the schedule fetch, with no alternative — the agent
    # could only say "that's too soon, pick another time", a blind guessing
    # game that lost Call A (31559053: 4 PM too soon → caller asked twice for
    # guidance → hung up). It now pairs the rejection with the earliest
    # bookable option (today, else the next opening tomorrow) so every "no"
    # arrives with a tool-licensed "but I could do X".
    if too_soon:
        requested_speech = format_slot_for_speech(requested_utc.isoformat(), tenant_timezone)
        viable_today = [
            s for s in all_slots
            if _slot_start_dt(s) >= now_utc + timedelta(hours=1)
        ]
        if viable_today:
            alt = viable_today[0]
            alt_speech = format_slot_for_speech(alt["start"], tenant_timezone)
            token = register_slot_token(deps, alt["start"], alt["end"])
            deps["_last_offered_token"] = token
            log_tool_call(deps, {
                "name": "check_slot",
                "success": True,
                "result": "too_soon_with_alternative",
                "date": date,
                "time": time_str,
                "slot_token": token,
            })
            state = (
                f"STATE:too_soon requested={requested_speech} min_notice=1h"
                f" earliest_today={alt_speech} token={token}"
                " | DIRECTIVE:say that time is too soon (one hour minimum) and in"
                " the same breath offer the earliest-today time above or another"
                " day; pass this token to book_appointment if the caller takes it."
            )
            deps["_last_tool_state"] = state
            return state

        next_open = _find_next_opening(deps, tenant, sched, tenant_timezone, date)
        if next_open is not None:
            alt_speech, token = next_open
            log_tool_call(deps, {
                "name": "check_slot",
                "success": True,
                "result": "too_soon_day_done_next_opening",
                "date": date,
                "time": time_str,
                "slot_token": token,
            })
            state = (
                f"STATE:too_soon requested={requested_speech} min_notice=1h"
                f" nothing_left_today=true next_open={alt_speech} token={token}"
                " | DIRECTIVE:say today can no longer be booked and in the same"
                " breath offer the next opening above or another day; pass this"
                " token to book_appointment if the caller takes it."
            )
            deps["_last_tool_state"] = state
            return state

        log_tool_call(deps, {
            "name": "check_slot",
            "success": True,
            "result": "too_soon_no_alternative",
            "date": date,
            "time": time_str,
        })
        state = (
            f"STATE:too_soon requested={requested_speech} min_notice=1h"
            " nothing_left_today=true"
            " | DIRECTIVE:say today can no longer be booked and ask which other"
            " day works; do not fabricate times."
        )
        deps["_last_tool_state"] = state
        return state
    requested_end = requested_utc + timedelta(minutes=slot_duration)
    del requested_end  # reserved for a future exact-end check; kept for readability

    matched = None
    for slot in all_slots:
        s_iso = slot["start"].replace("Z", "+00:00") if slot["start"].endswith("Z") else slot["start"]
        e_iso = slot["end"].replace("Z", "+00:00") if slot["end"].endswith("Z") else slot["end"]
        s_dt = datetime.fromisoformat(s_iso)
        e_dt = datetime.fromisoformat(e_iso)
        if s_dt <= requested_utc < e_dt:
            matched = slot
            break

    if matched:
        speech = format_slot_for_speech(matched["start"], tenant_timezone)
        token = register_slot_token(deps, matched["start"], matched["end"])
        # Defense-in-depth for book_appointment when Gemini forgets the token.
        deps["_last_offered_token"] = token
        log_tool_call(deps, {
            "name": "check_slot",
            "success": True,
            "result": "available",
            "date": date,
            "time": time_str,
            "slot_token": token,
        })
        state = (
            f"STATE:slot_ok token={token} speech={speech}"
            " | DIRECTIVE:offer the time, ask to book. Pass this token to book_appointment."
        )
        deps["_last_tool_state"] = state
        return state

    # Not available — closest 3 alternatives same day.
    nearby = []
    for slot in all_slots:
        s_iso = slot["start"].replace("Z", "+00:00") if slot["start"].endswith("Z") else slot["start"]
        s_dt = datetime.fromisoformat(s_iso)
        nearby.append((abs((s_dt - requested_utc).total_seconds()), slot))
    nearby.sort(key=lambda p: p[0])
    closest = [s for _, s in nearby[:3]]

    date_label = format_date_label(date, tenant_timezone)
    requested_speech = format_slot_for_speech(requested_utc.isoformat(), tenant_timezone)

    if not closest:
        biz = tenant.get("business_name") or "the team"
        # P1: pair the "nothing that day" rejection with the next opening so
        # the caller is never sent back to blind guessing.
        next_open = _find_next_opening(deps, tenant, sched, tenant_timezone, date)
        if next_open is not None:
            alt_speech, token = next_open
            log_tool_call(deps, {
                "name": "check_slot",
                "success": True,
                "result": "day_empty_next_opening",
                "date": date,
                "time": time_str,
                "slot_token": token,
            })
            state = (
                f"STATE:day_empty requested={requested_speech} date_label={date_label}"
                f" business_name={biz} next_open={alt_speech} token={token}"
                " | DIRECTIVE:tell the caller nothing is open that day and in the"
                " same breath offer the next opening above or another day; pass"
                " this token to book_appointment if the caller takes it."
            )
            deps["_last_tool_state"] = state
            return state
        log_tool_call(deps, {
            "name": "check_slot",
            "success": True,
            "result": "day_empty",
            "date": date,
            "time": time_str,
        })
        state = (
            f"STATE:day_empty requested={requested_speech} date_label={date_label} business_name={biz}"
            " | DIRECTIVE:tell the caller nothing is open that day; offer another day or capture_lead."
        )
        deps["_last_tool_state"] = state
        return state

    # Alternatives branch: no single "last offered" — caller must pick.
    deps.pop("_last_offered_token", None)

    alt_parts = []
    alt_tokens = []
    for i, slot in enumerate(closest, 1):
        sp = format_slot_for_speech(slot["start"], tenant_timezone)
        tok = register_slot_token(deps, slot["start"], slot["end"])
        alt_tokens.append(tok)
        alt_parts.append(f"{i}.{sp} token={tok}")

    log_tool_call(deps, {
        "name": "check_slot",
        "success": True,
        "result": "slot_taken_with_alternatives",
        "date": date,
        "time": time_str,
        "slot_tokens": alt_tokens,
    })
    state = (
        f"STATE:slot_taken requested={requested_speech} alts={len(closest)}"
        f" | ALTS: {'; '.join(alt_parts)}"
        " | DIRECTIVE:offer one or two alternatives; ask which they want; pass that alt's token to book_appointment."
    )
    deps["_last_tool_state"] = state
    return state
