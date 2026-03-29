"""
transfer_call tool -- SIP REFER transfer to business owner.
Ported from src/tools/transfer-call.js -- same logic, same behavior.
Uses LiveKit SIP transfer instead of retell.call.transfer().
"""

import asyncio
import logging

from livekit import api
from livekit.agents import function_tool, RunContext

from ..lib.whisper_message import build_whisper_message

logger = logging.getLogger(__name__)


def create_transfer_call_tool(deps: dict):
    @function_tool(
        name="transfer_call",
        description=(
            "Transfer the current call to the business owner's phone number. "
            "Use when the caller explicitly requests a human, or after 3 failed clarification attempts. "
            "Always capture caller info (name, phone, issue) first, unless the caller explicitly requests immediate transfer."
        ),
    )
    async def transfer_call(
        context: RunContext,
        caller_name: str = "",
        job_type: str = "",
        urgency: str = "routine",
        summary: str = "",
        reason: str = "caller_requested",
    ) -> str:
        owner_phone = deps.get("owner_phone")
        supabase = deps["supabase"]

        if not owner_phone:
            return "transfer_unavailable"

        # Write exception_reason to calls record
        exception_reason = reason or (
            "clarification_limit"
            if summary and "clarif" in summary.lower()
            else "caller_requested"
        )

        await asyncio.to_thread(
            lambda: supabase.table("calls").update(
                {"exception_reason": exception_reason}
            ).eq("call_id", deps.get("call_id", "")).execute()
        )

        # Build whisper context (spoken by agent before transfer for context)
        whisper_context = build_whisper_message(
            caller_name=caller_name or None,
            job_type=job_type or None,
            urgency=urgency or None,
            summary=summary or None,
        )
        logger.info("[agent] Transfer context: %s", whisper_context)

        # Perform SIP REFER transfer via LiveKit
        try:
            lk = api.LiveKitAPI()
            await lk.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    participant_identity=deps["sip_participant_identity"],
                    room_name=deps["room_name"],
                    transfer_to=f"sip:{owner_phone}@pstn.twilio.com",
                )
            )
            await lk.aclose()
            return "transfer_initiated"

        except Exception as err:
            logger.error("[agent] Transfer failed: %s", str(err))
            return "transfer_failed"

    return transfer_call
