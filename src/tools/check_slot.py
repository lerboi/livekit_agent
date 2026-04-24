"""
check_slot -- verify whether a specific (date, time) is bookable.

Replaces the date+time branch of the former monolithic check_availability.
Uses raw_schema so Gemini's serializer enforces `required: [date, time]`
and the HH:MM / YYYY-MM-DD patterns before invocation. That structural
guarantee is what moved Phase 63.1-08/09/10 bug-classes out of prose.

Returns a short STATE+DIRECTIVE string (≤150 chars in the common path) so
Gemini 3.1 Flash Live can begin audio generation with minimal ingestion lag.
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
            return "STATE:lookup_failed | DIRECTIVE:apologize briefly; offer capture_lead; do not retry."

    return check_slot


async def _impl(deps: dict, date: str, time_str: str, urgency: str) -> str:
    tenant_id = deps.get("tenant_id")
    if not tenant_id:
        return "STATE:lookup_failed reason=no_tenant | DIRECTIVE:apologize briefly; offer capture_lead."

    if not date or not time_str:
        # Schema makes this unreachable from Gemini, but belt-and-braces.
        return "STATE:missing_args | DIRECTIVE:ask the caller for a specific date and time."

    tenant = await ensure_tenant(deps)
    if not tenant:
        return "STATE:lookup_failed reason=tenant | DIRECTIVE:apologize briefly; offer capture_lead."
    tenant_timezone = tenant.get("tenant_timezone") or "UTC"
    slot_duration = tenant.get("slot_duration_mins") or 60

    today_local = tenant_today(tenant_timezone)
    if date < today_local:
        return (
            f"STATE:past_date requested={date} today={today_local}"
            " | DIRECTIVE:ask for today or later; do not fabricate times."
        )

    requested_utc = parse_hhmm_to_utc(time_str, date, tenant_timezone)
    if requested_utc is None:
        return "STATE:bad_time_format | DIRECTIVE:ask the caller to restate the time (e.g. '2 PM' or '14:00')."

    now_utc = datetime.now(timezone.utc)
    if requested_utc < now_utc + timedelta(hours=1) and date == today_local:
        speech = format_slot_for_speech(requested_utc.isoformat(), tenant_timezone)
        return (
            f"STATE:too_soon requested={speech} min_notice=1h"
            " | DIRECTIVE:tell the caller that time is too soon (one hour minimum); ask for later today or another day."
        )

    sched = await fetch_scheduling_data(deps)
    if sched is None:
        return "STATE:lookup_failed reason=scheduling_data | DIRECTIVE:apologize briefly; offer capture_lead."

    all_slots = calc_slots_for_dates(tenant, [date], sched, tenant_timezone)
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
        return (
            f"STATE:slot_ok token={token} speech={speech}"
            " | DIRECTIVE:offer the time, ask to book. Pass this token to book_appointment."
        )

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
        log_tool_call(deps, {
            "name": "check_slot",
            "success": True,
            "result": "day_empty",
            "date": date,
            "time": time_str,
        })
        return (
            f"STATE:day_empty requested={requested_speech} date_label={date_label} business_name={biz}"
            " | DIRECTIVE:tell the caller nothing is open that day; offer another day or capture_lead."
        )

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
    return (
        f"STATE:slot_taken requested={requested_speech} alts={len(closest)}"
        f" | ALTS: {'; '.join(alt_parts)}"
        " | DIRECTIVE:offer one or two alternatives; ask which they want; pass that alt's token to book_appointment."
    )
