"""
end_call tool -- graceful call termination.
Ported from src/tools/end-call.js -- same logic, same behavior.
Gemini generates the farewell, then we disconnect the SIP participant.
"""

import asyncio
import logging

from livekit import api
from livekit.agents import function_tool, RunContext

logger = logging.getLogger(__name__)


async def _delayed_disconnect(deps: dict) -> None:
    """Wait 3 seconds for farewell audio to play, then remove the SIP participant."""
    await asyncio.sleep(3)
    try:
        lk = api.LiveKitAPI()
        await lk.room.remove_participant(
            api.RoomParticipantIdentity(
                room=deps["room_name"],
                identity=deps["sip_participant_identity"],
            )
        )
        await lk.aclose()
    except Exception as e:
        logger.error("[agent] Failed to disconnect SIP participant: %s", str(e))


def create_end_call_tool(deps: dict):
    @function_tool(
        name="end_call",
        description=(
            "End the call gracefully after all actions are complete. "
            "Always capture caller information before using this if no booking was made."
        ),
    )
    async def end_call(context: RunContext) -> str:
        # Gemini will generate the farewell from prompt instructions.
        # After farewell is spoken, disconnect the SIP participant.
        # Use a short delay to let the farewell audio play out.
        asyncio.create_task(_delayed_disconnect(deps))
        return "Call ending."

    return end_call
