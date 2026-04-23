"""
check_caller_history tool -- repeat caller awareness.
Ported from src/tools/check-caller-history.js -- same logic, same behavior.
Read-only -- no database writes.
"""

import asyncio
import logging
from datetime import datetime, timezone

from livekit.agents import function_tool, RunContext

from ..utils import format_slot_for_speech

logger = logging.getLogger(__name__)


def create_check_caller_history_tool(deps: dict):
    @function_tool(
        name="check_caller_history",
        description=(
            "Check caller history for repeat caller awareness. No parameters needed. "
            "Invoke after greeting, before first question."
        ),
    )
    async def check_caller_history(context: RunContext) -> str:
        tenant_id = deps.get("tenant_id")
        from_number = deps.get("from_number")
        supabase = deps["supabase"]

        if not tenant_id or not from_number:
            return (
                "STATE:history_lookup_failed"
                " | DIRECTIVE:proceed with normal intake; do not apologize or mention the"
                " failure to the caller; do not recite any history."
            )

        # Look up tenant timezone for formatting
        try:
            tenant_result = await asyncio.to_thread(
                lambda: supabase.table("tenants")
                .select("tenant_timezone")
                .eq("id", tenant_id)
                .single()
                .execute()
            )
            tenant = tenant_result.data if tenant_result.data else None
        except Exception as e:
            logger.error("[agent] check_caller_history: tenant fetch failed: %s", e)
            return (
                "STATE:history_lookup_failed"
                " | DIRECTIVE:proceed with normal intake; do not apologize or mention the"
                " failure to the caller; do not recite any history."
            )

        tenant_timezone = tenant.get("tenant_timezone") if tenant else None
        if not tenant_timezone:
            logger.warning(
                "[tenant_config] null tenant_timezone tenant_id=%s — falling back to UTC; "
                "caller times may be misaligned; backfill tenants.tenant_timezone to fix",
                tenant_id,
            )
            tenant_timezone = "UTC"

        now_iso = datetime.now(timezone.utc).isoformat()

        # Phase 59: Lookup via customers → jobs/inquiries instead of legacy leads.
        # Resolve the customer by (tenant_id, phone_e164); if present, fetch their
        # last 3 interactions across jobs + inquiries. Parallel with upcoming
        # appointments (appointments.caller_phone is the E.164 caller number).
        from ..lib.phone import _normalize_phone
        phone_e164 = _normalize_phone(from_number) if from_number else None

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
            logger.error("[agent] check_caller_history: history lookup failed: %s", e)
            return (
                "STATE:history_lookup_failed"
                " | DIRECTIVE:proceed with normal intake; do not apologize or mention the"
                " failure to the caller; do not recite any history."
            )

        customer = (customer_result.data or [None])[0]
        appointments = appointments_result.data or []

        # Fetch last 3 jobs + inquiries for this customer (if any)
        interactions: list[dict] = []
        if customer:
            try:
                jobs_result, inquiries_result = await asyncio.gather(
                    asyncio.to_thread(
                        # Note: `jobs` has no `job_type` column (it lives on the
                        # linked `appointments` row for booked work). We read
                        # status + timestamp only here; for repeat-caller context
                        # the job type isn't essential — status is what matters.
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
                # Merge + sort by created_at desc, keep top 3 overall
                merged: list[dict] = []
                for row in (jobs_result.data or []):
                    merged.append({"kind": "job", **row})
                for row in (inquiries_result.data or []):
                    merged.append({"kind": "inquiry", **row})
                merged.sort(key=lambda r: r.get("created_at") or "", reverse=True)
                interactions = merged[:3]
            except Exception as e:
                logger.error("[agent] check_caller_history: interactions lookup failed: %s", e)
                # Soft-fail — proceed with whatever we have (customer + appointments).

        if not customer and len(appointments) == 0:
            return (
                "STATE:first_time_caller"
                " | DIRECTIVE:proceed with normal intake; do not mention that the caller is"
                " new; do not recite any history."
            )

        # Build natural-language summary for the AI
        summary = ""

        if len(appointments) > 0:
            appt_lines = []
            for a in appointments:
                date_str = format_slot_for_speech(a["start_time"], tenant_timezone)
                addr = a.get("service_address") or "address on file"
                status = a.get("status", "unknown")
                appt_lines.append(f"- {date_str} at {addr} ({status})")
            summary += f"Upcoming appointments:\n" + "\n".join(appt_lines) + "\n\n"

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

    return check_caller_history
