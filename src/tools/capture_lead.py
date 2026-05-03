"""
capture_lead tool -- saves caller info as a lead when they decline booking.
Ported from src/tools/capture-lead.js -- same logic, same behavior.
"""

import asyncio
import logging
import time

from livekit.agents import function_tool, RunContext

from ..lib.write_outcome import record_outcome, RecordOutcomeError
from ..integrations.google_maps import validate_address_bounded

logger = logging.getLogger(__name__)


def create_capture_lead_tool(deps: dict):
    @function_tool(
        name="capture_lead",
        description=(
            "Capture the caller's contact information and intent when they decline to book (the decline"
            " path). CRITICAL PRECONDITIONS: (1) gather the caller's name, the service issue, and the"
            " service address using the same single-question address rule as the booking path — ask one"
            " natural question ('What\\'s the address where you need the service?'), loop one targeted"
            " follow-up at a time, capture enough to find the place; (2) read back the name (if captured)"
            " and full address once before calling this tool (same readback rule as book_appointment)."
            " Do not call this tool until both preconditions are met. This tool's return is a"
            " state+directive string — do not read it aloud."
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
            return (
                "STATE:lead_capture_failed reason=no_tenant_id"
                " | DIRECTIVE:apologize briefly; tell the caller someone will follow up; do not"
                " attempt to capture again."
            )

        # Compute mid-call duration from start_timestamp (milliseconds) (avoids 15s filter issue)
        start_timestamp = deps.get("start_timestamp") or int(time.time() * 1000)
        duration_seconds = round((time.time() * 1000 - start_timestamp) / 1000)

        # Combine street_name + unit_number + postal_code into service_address
        parts = [p for p in [street_name, unit_number, postal_code] if p]
        service_address = ", ".join(parts) if parts else None

        # Phase 61 (D-B4): symmetric validation pre-check — same shape as book_appointment.
        # NOTE: `tenant_id` is already in local scope (extracted above via
        # `tenant_id = deps.get("tenant_id")`). Use the existing local `tenant_id`
        # directly — do NOT refetch from `deps`.
        region_code = (deps.get("country") or "US").upper()
        address_lines_for_validation = (
            [", ".join(p for p in [street_name, unit_number] if p)]
            if (street_name or unit_number)
            else []
        )

        validation_result = await validate_address_bounded(
            tenant_id=tenant_id,
            call_id=deps.get("call_id"),
            region_code=region_code,
            address_lines=address_lines_for_validation,
            postal_code=postal_code or None,
            locality=None,
            supabase=supabase,
            timeout_seconds=1.5,
        )

        validation_verdict = validation_result.get("verdict", "error")
        formatted_address_value = validation_result.get("formatted_address")
        # D-D3' inquiries.service_address overwrite (same column name as appointments)
        if validation_verdict in ("confirmed", "confirmed_with_changes") and formatted_address_value:
            service_address = formatted_address_value

        call_uuid = deps.get("call_uuid")
        if not call_uuid:
            # Background db_task hasn't written the calls row yet — rare; fail closed
            # for this tool since record_call_outcome RPC requires a valid call UUID.
            return (
                "STATE:lead_capture_failed reason=call_not_ready"
                " | DIRECTIVE:apologize briefly; tell the caller someone will follow up;"
                " do not attempt to capture again."
            )

        try:
            # Phase 59 D-10 inquiry path: appointment_id=None → record_call_outcome
            # upserts the customer and creates an inquiry row (not a job). No direct
            # writes to legacy leads/lead_calls (D-02a).
            await record_outcome(
                supabase,
                tenant_id=tenant_id,
                raw_phone=deps.get("from_number") or phone or "",
                caller_name=caller_name or None,
                service_address=service_address,
                appointment_id=None,
                urgency="routine",
                call_id=call_uuid,
                job_type=job_type or None,
                # Phase 61 NEW — pass validation result fields through:
                formatted_address=validation_result.get("formatted_address"),
                place_id=validation_result.get("place_id"),
                latitude=validation_result.get("latitude"),
                longitude=validation_result.get("longitude"),
                address_components=validation_result.get("address_components"),
                address_validation_verdict=validation_verdict,
            )

            # Write booking_outcome: 'declined' (conditional -- don't overwrite 'booked')
            await asyncio.to_thread(
                lambda: supabase.table("calls").update(
                    {"booking_outcome": "declined"}
                ).eq("call_id", deps.get("call_id", "")).is_("booking_outcome", "null").execute()
            )

            # Phase 61 D-E2: verdict-driven STATE+DIRECTIVE return string. The agent
            # reads these in the prompt (Plan 04) and decides how to speak the result;
            # the strings themselves are NEVER spoken aloud verbatim.
            formatted_address_for_return = validation_result.get("formatted_address")
            if validation_verdict == "confirmed":
                return (
                    f"LEAD CAPTURED [verdict=validated]: relay normalized address "
                    f"[{formatted_address_for_return}] as confirmed; "
                    f"ask if anything else is needed"
                )
            elif validation_verdict == "confirmed_with_changes":
                return (
                    f"LEAD CAPTURED [verdict=validated_with_corrections]: relay normalized address "
                    f"[{formatted_address_for_return}] as the final form, "
                    f"explicitly invite caller confirmation; "
                    f"if caller corrects, accept correction and re-read full address"
                )
            else:
                # unconfirmed | error | skipped | unsupported_region
                return (
                    "LEAD CAPTURED [verdict=unvalidated]: relay address as caller spoke it; "
                    "do NOT claim \"validated\", \"confirmed against records\", or "
                    "\"looked up your address\""
                )

        except Exception as err:
            logger.error("[agent] capture_lead error: %s", str(err))
            return (
                "STATE:lead_capture_failed reason=db_error"
                " | DIRECTIVE:apologize briefly; assure the caller that someone will follow up;"
                " do not attempt to capture again in this call."
            )

    return capture_lead
