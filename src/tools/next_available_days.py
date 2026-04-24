"""
next_available_days -- yes/no whether the business has any slots in the next
3 days. For callers who won't name a date ("whenever works"). No args, no
times, never. The agent then asks the caller to name a day.
"""

from __future__ import annotations

import logging
import time as _time

from livekit.agents import function_tool, RunContext

from ._availability_lib import (
    calc_slots_for_dates,
    ensure_tenant,
    fetch_scheduling_data,
    log_tool_call,
    next_n_local_dates,
)

logger = logging.getLogger(__name__)


_SCHEMA = {
    "name": "next_available_days",
    "description": (
        "Check whether the business has any availability in the next 3 days. "
        "Use only when the caller is vague about when ('whenever works', "
        "'anytime'). Returns yes/no — never specific times or dates. Speak a "
        "short filler phrase first ('Let me see what's coming up'), then invoke "
        "in the same turn. This tool's return is a state+directive string — do not read it aloud."
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
            return "STATE:lookup_failed | DIRECTIVE:apologize briefly; offer capture_lead; do not retry."

    return next_available_days


async def _impl(deps: dict) -> str:
    if not deps.get("tenant_id"):
        return "STATE:lookup_failed reason=no_tenant | DIRECTIVE:apologize briefly; offer capture_lead."

    tenant = await ensure_tenant(deps)
    if not tenant:
        return "STATE:lookup_failed reason=tenant | DIRECTIVE:apologize briefly; offer capture_lead."
    tenant_timezone = tenant.get("tenant_timezone") or "UTC"

    sched = await fetch_scheduling_data(deps)
    if sched is None:
        return "STATE:lookup_failed reason=scheduling_data | DIRECTIVE:apologize briefly; offer capture_lead."

    dates = next_n_local_dates(3, tenant_timezone)
    all_slots = calc_slots_for_dates(tenant, dates, sched, tenant_timezone)

    if all_slots:
        log_tool_call(deps, {"name": "next_available_days", "success": True, "result": "has_slots"})
        return (
            "STATE:has_near_availability"
            " | DIRECTIVE:tell the caller we have openings soon; ask them to name a specific day; do not mention times."
        )

    biz = tenant.get("business_name") or "the team"
    log_tool_call(deps, {"name": "next_available_days", "success": True, "result": "empty"})
    return (
        f"STATE:no_near_availability business_name={biz}"
        " | DIRECTIVE:tell the caller the next few days look full; offer capture_lead so they can call back."
    )
