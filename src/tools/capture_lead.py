"""
capture_lead tool -- saves caller info as a lead when they decline booking.
Ported from src/tools/capture-lead.js -- same logic, same behavior.
"""

import asyncio
import logging
import time

from livekit.agents import function_tool, RunContext

from ..lib.write_outcome import record_outcome, RecordOutcomeError
from ..integrations.google_maps import validate_address_with_region_fallback
from ..integrations.jobber import _normalize_free_form
from .validate_address import get_cached_validation

logger = logging.getLogger(__name__)


def create_capture_lead_tool(deps: dict):
    @function_tool(
        name="capture_lead",
        # Phase 61 Plan 04 (D-E1): description encodes the address-validation
        # precondition as outcome-framed prompt-surface language, symmetric
        # with book_appointment. Verdict-driven readback rule (D-E3) lives in
        # the prompt CRITICAL RULE block.
        description=(
            "Capture the caller's contact information and intent when they decline to book."
            " CRITICAL PRECONDITIONS: (1) gather the caller's name, the service issue, and the"
            " service address using the same single-question address rule as the booking path — ask one"
            " natural question ('What\\'s the address where you need the service?'), loop one targeted"
            " follow-up at a time, capture enough to find the place; (2) validate the address with"
            " validate_address the moment the caller gives it and confirm the result back once — if"
            " validate_address was never called, this tool performs the same validation itself as a"
            " fallback. The tool return will indicate whether the address was confirmed, corrected, or"
            " could not be verified, and will tell you what to speak back to the caller. Speak only what"
            " the return tells you. Do not call this tool until both preconditions are met. This tool's"
            " return is a state+directive string — do not read it aloud."
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
            state = (
                "STATE:lead_capture_failed reason=no_tenant_id"
                " | DIRECTIVE:apologize briefly; tell the caller someone will follow up; do not"
                " attempt to capture again."
            )
            deps["_last_tool_state"] = state
            return state

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

        # 2026-06-10 early-validation reuse: same contract as book_appointment —
        # reuse the mid-call validate_address result when the input matches
        # (normalized street + postal; unit tolerated); else validate as before.
        cached_validation = get_cached_validation(deps, street_name, postal_code)
        used_cached_validation = cached_validation is not None
        if used_cached_validation:
            logger.info(
                "[capture_lead] reusing mid-call validate_address result "
                "(verdict=%s) for call=%s",
                cached_validation.get("verdict"),
                deps.get("call_id"),
            )
            validation_result = cached_validation
        else:
            # Tenant region first; automatic caller-region (caller-ID) second
            # attempt only when the first verdict is unconfirmed/unsupported —
            # up to 1.5s extra on that rare path only (see google_maps).
            validation_result, _validation_region = await validate_address_with_region_fallback(
                tenant_id=tenant_id,
                call_id=deps.get("call_id"),
                region_code=region_code,
                caller_region=deps.get("caller_region"),
                address_lines=address_lines_for_validation,
                postal_code=postal_code or None,
                locality=None,
                supabase=supabase,
                timeout_seconds=1.5,
            )
            if _validation_region != region_code:
                logger.info(
                    "[capture_lead] address validated with region=%s "
                    "(tenant region=%s) call=%s",
                    _validation_region, region_code, deps.get("call_id"),
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
            state = (
                "STATE:lead_capture_failed reason=call_not_ready"
                " | DIRECTIVE:apologize briefly; tell the caller someone will follow up;"
                " do not attempt to capture again."
            )
            deps["_last_tool_state"] = state
            return state

        # Prefer an explicitly captured callback number over caller-ID when the
        # model passed one AND it parses to a plausible E.164 — callers who say
        # "reach me on my other number" were silently losing it (from_number
        # always won). _normalize_free_form (phonenumbers, tenant-country
        # default region) handles spoken/free-form shapes that the SIP-attr
        # normalizer in src/lib/phone.py does not; on parse failure we fall
        # back to caller-ID as before.
        provided_phone = _normalize_free_form(phone, region_code) if phone else None
        raw_phone = provided_phone or deps.get("from_number") or phone or ""

        # The record_call_outcome RPC (migration 062, 14-arg) has no notes-like
        # parameter and inquiries has no notes column — fold notes into the
        # job_type free-text the RPC writes onto the inquiry row so the promised
        # data is persisted rather than dropped.
        job_type_value = job_type or None
        if notes:
            job_type_value = f"{job_type_value} — {notes}" if job_type_value else notes

        try:
            # Phase 59 D-10 inquiry path: appointment_id=None → record_call_outcome
            # upserts the customer and creates an inquiry row (not a job). No direct
            # writes to legacy leads/lead_calls (D-02a).
            await record_outcome(
                supabase,
                tenant_id=tenant_id,
                raw_phone=raw_phone,
                caller_name=caller_name or None,
                service_address=service_address,
                appointment_id=None,
                urgency="routine",
                call_id=call_uuid,
                job_type=job_type_value,
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

            # Phase 61 D-E2 verdict-driven return strings, SHORTENED 2026-06-10
            # for the early-validation flow: when the address was already
            # validated and confirmed mid-call via validate_address
            # (used_cached_validation), the post-capture confirmation is ONE
            # short sentence — no address re-read. The address only re-enters
            # the directive on the fallback path (this tool validated it
            # itself, so the caller never heard the final form). The verdict=
            # tokens are load-bearing (tests + prompt rule) — do not rename.
            # The agent reads these; they are NEVER spoken aloud verbatim.
            formatted_address_for_return = validation_result.get("formatted_address")
            if validation_verdict == "confirmed":
                if used_cached_validation:
                    state = (
                        "LEAD CAPTURED [verdict=validated]: details saved — confirm in "
                        "ONE short sentence that the team will follow up; the address "
                        "was already confirmed — do not re-read it; "
                        "ask if anything else is needed"
                    )
                else:
                    # 2026-06-11 (findings.md P2): fallback path no longer
                    # re-reads the normalized address — the caller already
                    # heard their address read back before this tool ran
                    # (mirrors book_appointment; Call B repetition failure).
                    state = (
                        "LEAD CAPTURED [verdict=validated]: confirm in ONE short "
                        "sentence that the team will follow up; do not re-read "
                        "the address — the caller already heard it; "
                        "ask if anything else is needed"
                    )
                deps["_last_tool_state"] = state
                return state
            elif validation_verdict == "confirmed_with_changes":
                if used_cached_validation:
                    state = (
                        "LEAD CAPTURED [verdict=validated_with_corrections]: details "
                        "saved — confirm in ONE short sentence that the team will "
                        "follow up; the corrected address was already confirmed — do "
                        "not re-read it; ask if anything else is needed"
                    )
                else:
                    state = (
                        f"LEAD CAPTURED [verdict=validated_with_corrections]: read "
                        f"corrected address [{formatted_address_for_return}] once and "
                        f"explicitly invite caller confirmation; "
                        f"if caller corrects, accept correction and re-read once"
                    )
                deps["_last_tool_state"] = state
                return state
            else:
                # unconfirmed | error | skipped | unsupported_region
                state = (
                    "LEAD CAPTURED [verdict=unvalidated]: confirm in ONE short sentence "
                    "that the team will follow up; "
                    "relay address as caller spoke it only if it was never read back; "
                    "do NOT claim \"validated\", \"confirmed against records\", or "
                    "\"looked up your address\""
                )
                deps["_last_tool_state"] = state
                return state

        except Exception as err:
            logger.error("[agent] capture_lead error: %s", str(err))
            state = (
                "STATE:lead_capture_failed reason=db_error"
                " | DIRECTIVE:apologize briefly; assure the caller that someone will follow up;"
                " do not attempt to capture again in this call."
            )
            deps["_last_tool_state"] = state
            return state

    return capture_lead
