"""
check_caller_history tool -- repeat caller awareness.
Ported from src/tools/check-caller-history.js -- same logic, same behavior.
Read-only -- no database writes.

Phase 62 (call AJ_bFP3MLdqnKqT, 2026-05-07): the eager-invoke pattern
("Invoke after greeting, before first question") created a 3-5s silent
gap on every call's first turn — caller spoke, tool fired, input muted
2-3s while Supabase round-trip ran, agent then responded. To eliminate
that first-turn latency, the fetch logic is extracted into module-level
helpers so the agent entrypoint can run it in parallel with
customer_context BEFORE session.start(). Result lands in the system
prompt as a STATE+DIRECTIVE block via prompt._build_caller_history_section.
The tool itself remains for mid-call queries when the caller explicitly
asks about prior interactions or their account.
"""

import asyncio
import logging
from datetime import datetime, timezone

from livekit.agents import function_tool, RunContext

from ._availability_lib import mute_input_during_tool
from ..utils import format_slot_for_speech

logger = logging.getLogger(__name__)


_HISTORY_LOOKUP_FAILED_STATE = (
    "STATE:history_lookup_failed"
    " | DIRECTIVE:proceed with normal intake; do not apologize or mention the"
    " failure to the caller; do not recite any history."
)

_FIRST_TIME_CALLER_STATE = (
    "STATE:first_time_caller"
    " | DIRECTIVE:proceed with normal intake; do not mention that the caller is"
    " new; do not recite any history."
)


async def fetch_caller_history(
    supabase,
    tenant_id: str,
    from_number: str,
    tenant_timezone: str = "UTC",
) -> dict | None:
    """Pure async fetcher — runs the customer+appointments+interactions
    queries and returns a dict ready for format_caller_history_state.

    Returns:
        dict with keys {customer, appointments, interactions, tenant_timezone}
            for repeat callers (any data found).
        {} (empty dict) for first-time callers (no customer, no appointments).
        None on fatal error (DB unreachable, etc.).

    Used by both the agent entrypoint (pre-session) and the tool wrapper
    (mid-call invocation). Same query shape as the original tool body.
    """
    if not tenant_id or not from_number:
        return None

    now_iso = datetime.now(timezone.utc).isoformat()

    # Phase 59: lookup via customers → jobs/inquiries instead of legacy leads.
    from ..lib.phone import _normalize_phone
    phone_e164 = _normalize_phone(from_number)

    try:
        customer_result, appointments_result = await asyncio.gather(
            asyncio.to_thread(
                lambda: supabase.table("customers")
                .select("id, name")
                .eq("tenant_id", tenant_id)
                .eq("phone_e164", phone_e164)
                .limit(1)
                .execute()
            ),
            asyncio.to_thread(
                lambda: supabase.table("appointments")
                .select("start_time, end_time, service_address, status, caller_name")
                .eq("tenant_id", tenant_id)
                .eq("caller_phone", from_number)
                .neq("status", "cancelled")
                .gte("end_time", now_iso)
                .order("start_time")
                .limit(3)
                .execute()
            ),
        )
    except Exception as e:
        logger.error("[caller_history] customer/appointments lookup failed: %s", e)
        return None

    customer = (customer_result.data or [None])[0]
    appointments = appointments_result.data or []

    interactions: list[dict] = []
    if customer:
        try:
            jobs_result, inquiries_result = await asyncio.gather(
                asyncio.to_thread(
                    # Note: `jobs` has no `job_type` column (it lives on the
                    # linked `appointments` row for booked work). We read
                    # status + timestamp only; for repeat-caller context the
                    # job type isn't essential — status is what matters.
                    lambda: supabase.table("jobs")
                    .select("status, created_at")
                    .eq("tenant_id", tenant_id)
                    .eq("customer_id", customer["id"])
                    .order("created_at", desc=True)
                    .limit(3)
                    .execute()
                ),
                asyncio.to_thread(
                    lambda: supabase.table("inquiries")
                    .select("job_type, status, created_at")
                    .eq("tenant_id", tenant_id)
                    .eq("customer_id", customer["id"])
                    .order("created_at", desc=True)
                    .limit(3)
                    .execute()
                ),
            )
            merged: list[dict] = []
            for row in (jobs_result.data or []):
                merged.append({"kind": "job", **row})
            for row in (inquiries_result.data or []):
                merged.append({"kind": "inquiry", **row})
            merged.sort(key=lambda r: r.get("created_at") or "", reverse=True)
            interactions = merged[:3]
        except Exception as e:
            logger.error("[caller_history] interactions lookup failed: %s", e)
            # Soft-fail — proceed with whatever we have.

    if not customer and len(appointments) == 0:
        # First-time caller: empty marker dict (distinguishable from None,
        # which means fatal fetch error).
        return {}

    return {
        "customer": customer,
        "appointments": appointments,
        "interactions": interactions,
        "tenant_timezone": tenant_timezone,
    }


def format_caller_history_state(history: dict | None) -> str:
    """Render the history dict to a STATE+DIRECTIVE string. Suitable for
    both system-prompt injection (pre-session) and tool return (mid-call).

    Maps:
        None             → STATE:history_lookup_failed
        {}               → STATE:first_time_caller
        {customer, ...}  → STATE:repeat_caller + summary + DIRECTIVE
    """
    if history is None:
        return _HISTORY_LOOKUP_FAILED_STATE
    if not history:
        return _FIRST_TIME_CALLER_STATE

    customer = history.get("customer")
    appointments = history.get("appointments") or []
    interactions = history.get("interactions") or []
    tenant_timezone = history.get("tenant_timezone") or "UTC"

    summary = ""

    if appointments:
        appt_lines = []
        for a in appointments:
            date_str = format_slot_for_speech(a["start_time"], tenant_timezone)
            addr = a.get("service_address") or "address on file"
            status = a.get("status", "unknown")
            appt_lines.append(f"- {date_str} at {addr} ({status})")
        summary += "Upcoming appointments:\n" + "\n".join(appt_lines) + "\n\n"

    if interactions:
        interaction_lines = []
        name = (customer or {}).get("name") or "Unknown"
        for i in interactions:
            kind = i.get("kind", "inquiry")
            job = i.get("job_type") or "unspecified"
            status = i.get("status", "unknown")
            interaction_lines.append(f"- {kind}: {job} (status: {status})")
        summary += f"Previous interactions ({name}):\n" + "\n".join(interaction_lines)

    return (
        f"STATE:repeat_caller"
        f" prior_appointments={len(appointments)} prior_interactions={len(interactions)}"
        f"\nCONTEXT:\n{summary}\n"
        " | DIRECTIVE:use this context silently to personalize follow-up questions if"
        " relevant (e.g., 'is this about the same {last_service}?'); do not recite the"
        " caller's history; do not say you have their information on file; do not skip"
        " asking for their name, address, or any other details — ask every question as if"
        " this is the very first time they have called; only reference prior history if"
        " the caller explicitly says they have called before and asks whether you have"
        " their information."
    )


def create_check_caller_history_tool(deps: dict):
    @function_tool(
        name="check_caller_history",
        description=(
            "Check caller history for repeat-caller awareness — looks up prior "
            "appointments, jobs, and inquiries for this phone number. No "
            "parameters needed. Caller history is already provided in your "
            "initial context, so you do NOT need to invoke this at call start. "
            "Invoke ONLY when the caller explicitly asks about their account, "
            "their prior appointments, or whether you have their information."
        ),
    )
    async def check_caller_history(context: RunContext) -> str:
        # Phase 61.2 Fix A: detach caller input during the BLOCKING Supabase
        # fetch so Gemini-server VAD can't fire mid-tool-wait and cancel the
        # generation. See 61.2-RESEARCH.md § 4 fix A.
        mute_input_during_tool(deps)

        tenant_id = deps.get("tenant_id")
        from_number = deps.get("from_number")
        supabase = deps["supabase"]
        tenant_timezone = deps.get("tenant_timezone") or "UTC"

        if not tenant_id or not from_number:
            deps["_last_tool_state"] = _HISTORY_LOOKUP_FAILED_STATE
            return _HISTORY_LOOKUP_FAILED_STATE

        history = await fetch_caller_history(
            supabase, tenant_id, from_number, tenant_timezone
        )
        state = format_caller_history_state(history)
        deps["_last_tool_state"] = state
        return state

    return check_caller_history
