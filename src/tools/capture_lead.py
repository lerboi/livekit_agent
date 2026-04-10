"""
capture_lead tool -- saves caller info as a lead when they decline booking.
Ported from src/tools/capture-lead.js -- same logic, same behavior.
"""

import asyncio
import logging
import time

from livekit.agents import function_tool, RunContext

from ..lib.leads import create_or_merge_lead

logger = logging.getLogger(__name__)


def create_capture_lead_tool(deps: dict):
    @function_tool(
        name="capture_lead",
        description=(
            "Capture caller information as a lead when the caller has firmly declined booking. "
            "Always tell the caller you're noting their details before calling this tool. "
            "Use when you're confident they don't want to book right now and you're about to "
            "wrap up the call. Must be used before ending the call."
        ),
    )
    async def capture_lead(
        context: RunContext,
        caller_name: str,
        phone: str = "",
        street_name: str = "",
        unit_number: str = "",
        postal_code: str = "",
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

        # Combine street_name + unit_number + postal_code into service_address
        parts = [p for p in [street_name, unit_number, postal_code] if p]
        service_address = ", ".join(parts) if parts else None

        try:
            await create_or_merge_lead(
                supabase,
                tenant_id=tenant_id,
                call_id=deps.get("call_uuid"),
                from_number=deps.get("from_number") or phone or "",
                caller_name=caller_name or None,
                job_type=job_type or None,
                service_address=service_address,
                postal_code=postal_code or None,
                street_name=street_name or None,
                triage_result={"urgency": "routine"},
                appointment_id=None,
                call_duration=duration_seconds,
            )

            # Write booking_outcome: 'declined' (conditional -- don't overwrite 'booked')
            await asyncio.to_thread(
                lambda: supabase.table("calls").update(
                    {"booking_outcome": "declined"}
                ).eq("call_id", deps.get("call_id", "")).is_("booking_outcome", "null").execute()
            )

            # Look up business name for confirmation message
            tenant_result = await asyncio.to_thread(
                lambda: supabase.table("tenants")
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
