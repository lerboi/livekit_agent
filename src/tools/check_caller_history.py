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
            return "No caller history available."

        # Look up tenant timezone for formatting
        tenant_result = await asyncio.to_thread(
            lambda: supabase.table("tenants")
            .select("tenant_timezone")
            .eq("id", tenant_id)
            .single()
            .execute()
        )
        tenant = tenant_result.data if tenant_result.data else None
        tenant_timezone = (tenant.get("tenant_timezone") if tenant else None) or "America/Chicago"

        now_iso = datetime.now(timezone.utc).isoformat()

        # Parallel lookup: leads + appointments for this caller
        leads_result, appointments_result = await asyncio.gather(
            asyncio.to_thread(
                lambda: supabase.table("leads")
                .select("id, caller_name, job_type, service_address, status, created_at")
                .eq("tenant_id", tenant_id)
                .eq("from_number", from_number)
                .order("created_at", desc=True)
                .limit(3)
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

        leads = leads_result.data or []
        appointments = appointments_result.data or []

        if len(leads) == 0 and len(appointments) == 0:
            return "First-time caller. No prior history found."

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

        if len(leads) > 0:
            lead_lines = []
            for l in leads:
                name = l.get("caller_name") or "Unknown"
                job = l.get("job_type") or "unspecified"
                status = l.get("status", "unknown")
                lead_lines.append(f"- {name}: {job} (status: {status})")
            summary += f"Previous interactions:\n" + "\n".join(lead_lines)

        return (
            f"Returning caller. {summary}\n\n"
            "Acknowledge their history naturally. If they have an upcoming appointment, "
            "ask if this call is about that appointment or something new. If they have both "
            "an appointment AND an open lead, mention the appointment first, then ask if this "
            "is about that or a new issue."
        )

    return check_caller_history
