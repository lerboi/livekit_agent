"""
next_available_days -- which of the next 3 days have bookable slots.

2026-06-11 naturalness pass (findings.md P1): formerly returned a bare yes/no
("never specific times or dates") and told the agent to ask the caller to
name a day — a blind guessing game. It now returns the actual day labels
with availability so the agent can offer them ("Thursday and Friday both
have openings — any preference?"). Times still come from check_day /
check_slot; the anti-hallucination invariant (every spoken day/time comes
from a tool return) is unchanged.
"""

from __future__ import annotations

import logging
import time as _time

from livekit.agents import function_tool, RunContext

from ._availability_lib import (
    calc_slots_for_dates,
    ensure_tenant,
    fetch_scheduling_data,
    format_date_label,
    log_tool_call,
    next_n_local_dates,
)

logger = logging.getLogger(__name__)


_SCHEMA = {
    "name": "next_available_days",
    "description": (
        "Find which of the next 3 days have appointment availability. Use "
        "only when the caller is vague about when ('whenever works', "
        "'anytime'). Returns the open days — offer them and let the caller "
        "pick; then call check_day for the chosen day. Never invent times "
        "yourself. Speak a short filler phrase first ('Let me see what's "
        "coming up'), then invoke in the same turn. This tool's return is a "
        "state+directive string — do not read it aloud."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}


def create_next_available_days_tool(deps: dict):
    @function_tool(raw_schema=_SCHEMA)
    async def next_available_days(raw_arguments: dict, context: RunContext) -> str:
        t0 = _time.time()
        call_id = f"nad_{int(t0 * 1000) % 100000}"

        logger.info("[63.1-DIAG] next_available_days ENTRY id=%s", call_id)
        try:
            result = await _impl(deps)
            logger.info(
                "[63.1-DIAG] next_available_days EXIT id=%s elapsed_ms=%d len=%d preview=%r",
                call_id, int((_time.time() - t0) * 1000), len(result or ""), (result or "")[:180],
            )
            return result
        except Exception as exc:
            logger.error("[63.1-DIAG] next_available_days EXCEPTION id=%s err=%s", call_id, repr(exc))
            state = "STATE:lookup_failed | DIRECTIVE:apologize briefly; offer capture_lead; do not retry."
            deps["_last_tool_state"] = state
            return state

    return next_available_days


async def _impl(deps: dict) -> str:
    if not deps.get("tenant_id"):
        state = "STATE:lookup_failed reason=no_tenant | DIRECTIVE:apologize briefly; offer capture_lead."
        deps["_last_tool_state"] = state
        return state

    tenant = await ensure_tenant(deps)
    if not tenant:
        state = "STATE:lookup_failed reason=tenant | DIRECTIVE:apologize briefly; offer capture_lead."
        deps["_last_tool_state"] = state
        return state
    tenant_timezone = tenant.get("tenant_timezone") or "UTC"

    sched = await fetch_scheduling_data(deps)
    if sched is None:
        state = "STATE:lookup_failed reason=scheduling_data | DIRECTIVE:apologize briefly; offer capture_lead."
        deps["_last_tool_state"] = state
        return state

    dates = next_n_local_dates(3, tenant_timezone)
    open_days = []
    for d in dates:
        day_slots = calc_slots_for_dates(tenant, [d], sched, tenant_timezone)
        if day_slots:
            open_days.append(f"{format_date_label(d, tenant_timezone)} ({len(day_slots)} open)")

    if open_days:
        log_tool_call(deps, {
            "name": "next_available_days",
            "success": True,
            "result": "has_slots",
            "open_days": len(open_days),
        })
        state = (
            f"STATE:has_near_availability days={'; '.join(open_days)}"
            " | DIRECTIVE:offer these days naturally — never recite slot counts;"
            " once the caller picks a day, call check_day for it and offer its"
            " times; do not invent times yourself."
        )
        deps["_last_tool_state"] = state
        return state

    biz = tenant.get("business_name") or "the team"
    log_tool_call(deps, {"name": "next_available_days", "success": True, "result": "empty"})
    state = (
        f"STATE:no_near_availability business_name={biz}"
        " | DIRECTIVE:tell the caller the next few days look full; offer capture_lead so they can call back."
    )
    deps["_last_tool_state"] = state
    return state
