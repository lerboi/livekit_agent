"""
check_day -- yes/no whether a specific day has any bookable slots.

Never returns specific times (by design) — the caller must name a concrete
hour, then check_slot verifies it. Splits out of the former check_availability
`date-only` branch. Short return (~120 chars) keeps Gemini Live audio-gen lag low.
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
    tenant_today,
)

logger = logging.getLogger(__name__)


_SCHEMA = {
    "name": "check_day",
    "description": (
        "Check whether a specific day has any appointment slots available. "
        "Use when the caller names a date but not a time yet. Returns yes/no "
        "only — never specific times. Speak a short filler phrase first "
        "('Let me see what that day looks like'), then invoke in the same turn. "
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
            return "STATE:lookup_failed | DIRECTIVE:apologize briefly; offer capture_lead; do not retry."


    return check_day


async def _impl(deps: dict, date: str) -> str:
    if not deps.get("tenant_id"):
        return "STATE:lookup_failed reason=no_tenant | DIRECTIVE:apologize briefly; offer capture_lead."
    if not date:
        return "STATE:missing_args | DIRECTIVE:ask the caller for a specific date."

    tenant = await ensure_tenant(deps)
    if not tenant:
        return "STATE:lookup_failed reason=tenant | DIRECTIVE:apologize briefly; offer capture_lead."
    tenant_timezone = tenant.get("tenant_timezone") or "UTC"

    if date < tenant_today(tenant_timezone):
        return (
            f"STATE:past_date requested={date}"
            " | DIRECTIVE:ask for today or later; do not fabricate times."
        )

    sched = await fetch_scheduling_data(deps)
    if sched is None:
        return "STATE:lookup_failed reason=scheduling_data | DIRECTIVE:apologize briefly; offer capture_lead."

    all_slots = calc_slots_for_dates(tenant, [date], sched, tenant_timezone)
    date_label = format_date_label(date, tenant_timezone)

    if all_slots:
        log_tool_call(deps, {"name": "check_day", "success": True, "result": "has_slots", "date": date})
        return (
            f"STATE:day_has_slots date_label={date_label} count={len(all_slots)}"
            " | DIRECTIVE:confirm the day is open; ask for a concrete hour; do not mention times."
        )

    biz = tenant.get("business_name") or "the team"
    log_tool_call(deps, {"name": "check_day", "success": True, "result": "empty", "date": date})
    return (
        f"STATE:day_empty date_label={date_label} business_name={biz}"
        " | DIRECTIVE:tell the caller nothing is open that day; offer another day or capture_lead."
    )
