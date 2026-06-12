"""
check_day -- list bookable windows for a specific day.

2026-06-11 naturalness pass (findings.md P1): formerly returned yes/no only
("never specific times" was a Gemini-era anti-fabrication guard). That forced
callers into a blind guessing game — name a time, get rejected, guess again
(Call A 31559053 hung up inside that loop). The tool now returns up to 3
representative windows (each with a registered slot_token), and the prompt
licenses offering ONLY tool-returned times — the anti-hallucination invariant
("every spoken time comes from a tool return") is unchanged.
"""

from __future__ import annotations

import logging
import time as _time

from livekit.agents import function_tool, RunContext

from ..utils import format_slot_for_speech
from ._availability_lib import (
    calc_slots_for_dates,
    ensure_tenant,
    fetch_scheduling_data,
    format_date_label,
    log_tool_call,
    pick_spread as _pick_spread,
    register_slot_token,
    tenant_today,
)

logger = logging.getLogger(__name__)


_SCHEMA = {
    "name": "check_day",
    "description": (
        "Check a specific day's availability. Use when the caller names a "
        "date but not a time yet. Returns up to 3 open windows for the day, "
        "each with a slot_token — offer two or three of them naturally; a "
        "time the caller picks can be booked directly with its token. Speak "
        "a short filler phrase first ('Let me see what that day looks like'), "
        "then invoke in the same turn. This tool's return is a "
        "state+directive string — do not read it aloud."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "pattern": r"^\d{4}-\d{2}-\d{2}$",
                "description": "Target date as YYYY-MM-DD in the business's local timezone.",
            },
        },
        "required": ["date"],
    },
}


def create_check_day_tool(deps: dict):
    @function_tool(raw_schema=_SCHEMA)
    async def check_day(raw_arguments: dict, context: RunContext) -> str:
        t0 = _time.time()
        call_id = f"cd_{int(t0 * 1000) % 100000}"
        date = (raw_arguments.get("date") or "").strip()

        logger.info("[63.1-DIAG] check_day ENTRY id=%s date=%r", call_id, date)
        try:
            result = await _impl(deps, date)
            logger.info(
                "[63.1-DIAG] check_day EXIT id=%s elapsed_ms=%d len=%d preview=%r",
                call_id, int((_time.time() - t0) * 1000), len(result or ""), (result or "")[:180],
            )
            return result
        except Exception as exc:
            logger.error(
                "[63.1-DIAG] check_day EXCEPTION id=%s err=%s",
                call_id, repr(exc),
            )
            state = "STATE:lookup_failed | DIRECTIVE:apologize briefly; offer capture_lead; do not retry."
            deps["_last_tool_state"] = state
            return state


    return check_day


async def _impl(deps: dict, date: str) -> str:
    if not deps.get("tenant_id"):
        state = "STATE:lookup_failed reason=no_tenant | DIRECTIVE:apologize briefly; offer capture_lead."
        deps["_last_tool_state"] = state
        return state
    if not date:
        state = "STATE:missing_args | DIRECTIVE:ask the caller for a specific date."
        deps["_last_tool_state"] = state
        return state

    tenant = await ensure_tenant(deps)
    if not tenant:
        state = "STATE:lookup_failed reason=tenant | DIRECTIVE:apologize briefly; offer capture_lead."
        deps["_last_tool_state"] = state
        return state
    tenant_timezone = tenant.get("tenant_timezone") or "UTC"

    if date < tenant_today(tenant_timezone):
        state = (
            f"STATE:past_date requested={date}"
            " | DIRECTIVE:ask for today or later; do not fabricate times."
        )
        deps["_last_tool_state"] = state
        return state

    sched = await fetch_scheduling_data(deps)
    if sched is None:
        state = "STATE:lookup_failed reason=scheduling_data | DIRECTIVE:apologize briefly; offer capture_lead."
        deps["_last_tool_state"] = state
        return state

    all_slots = calc_slots_for_dates(tenant, [date], sched, tenant_timezone)
    date_label = format_date_label(date, tenant_timezone)

    if all_slots:
        options = _pick_spread(all_slots, 3)
        opt_parts = []
        opt_tokens = []
        for i, slot in enumerate(options, 1):
            speech = format_slot_for_speech(slot["start"], tenant_timezone)
            token = register_slot_token(deps, slot["start"], slot["end"])
            opt_tokens.append(token)
            opt_parts.append(f"{i}.{speech} token={token}")
        log_tool_call(deps, {
            "name": "check_day",
            "success": True,
            "result": "has_slots",
            "date": date,
            "slot_tokens": opt_tokens,
        })
        state = (
            f"STATE:day_has_slots date_label={date_label} count={len(all_slots)}"
            f" | OPTIONS: {'; '.join(opt_parts)}"
            " | DIRECTIVE:offer two or three of these times naturally — never recite"
            " a list; the caller may also name their own time (verify it with"
            " check_slot). A time the caller picks from these options books directly"
            " with its token."
        )
        deps["_last_tool_state"] = state
        return state

    biz = tenant.get("business_name") or "the team"
    log_tool_call(deps, {"name": "check_day", "success": True, "result": "empty", "date": date})
    state = (
        f"STATE:day_empty date_label={date_label} business_name={biz}"
        " | DIRECTIVE:tell the caller nothing is open that day; offer another day or capture_lead."
    )
    deps["_last_tool_state"] = state
    return state
