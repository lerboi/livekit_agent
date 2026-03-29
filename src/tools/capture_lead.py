"""
capture_lead tool -- saves caller info as a lead when they decline booking.
Ported from src/tools/capture-lead.js -- same logic, same behavior.
"""

import logging
import time

from livekit.agents import function_tool, RunContext

from src.lib.leads import create_or_merge_lead

logger = logging.getLogger(__name__)


def create_capture_lead_tool(deps: dict):
    @function_tool(
        name="capture_lead",
        description=(
            "Capture caller information as a lead when they decline booking. "
            "Use after the second explicit decline. Must be used before ending the call."
        ),
    )
    async def capture_lead(
        context: RunContext,
        caller_name: str,
        phone: str = "",
        address: str = "",
        job_type: str = "",
        notes: str = "",
    ) -> str:
        tenant_id = deps.get("tenant_id")
        supabase = deps["supabase"]

        if not tenant_id:
            return "I've noted your details and someone will follow up."

        # Compute mid-call duration from start_timestamp (milliseconds) (avoids 15s filter issue)
        start_timestamp = deps.get("start_timestamp") or int(time.time() * 1000)
        duration_seconds = round((time.time() * 1000 - start_timestamp) / 1000)

        try:
            await create_or_merge_lead(
                supabase,
                tenant_id=tenant_id,
                call_id=deps.get("call_uuid") or deps.get("call_id", ""),
                from_number=deps.get("from_number") or phone or "",
                caller_name=caller_name or None,
                job_type=job_type or None,
                service_address=address or None,
                triage_result={"urgency": "routine"},
                appointment_id=None,
                call_duration=duration_seconds,
            )

            # Write booking_outcome: 'declined' (conditional -- don't overwrite 'booked')
            supabase.table("calls").update(
                {"booking_outcome": "declined"}
            ).eq("call_id", deps.get("call_id", "")).is_("booking_outcome", "null").execute()

            # Look up business name for confirmation message
            tenant_result = (
                supabase.table("tenants")
                .select("business_name")
                .eq("id", tenant_id)
                .single()
                .execute()
            )
            tenant = tenant_result.data if tenant_result.data else None
            biz_name = (tenant.get("business_name") if tenant else None) or "our team"

            return f"I've saved your information. {biz_name} will reach out soon."

        except Exception as err:
            logger.error("[agent] capture_lead error: %s", str(err))
            return "I've noted your details and someone will follow up."

    return capture_lead
