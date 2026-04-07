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
    """Wait for farewell audio to finish playing, then remove the SIP participant."""
    await asyncio.sleep(12)
    lk = api.LiveKitAPI()
    try:
        await lk.room.remove_participant(
            api.RoomParticipantIdentity(
                room=deps["room_name"],
                identity=deps["sip_participant_identity"],
            )
        )
    except Exception as e:
        # 404 = participant already left (caller hung up first) — expected, not an error
        if "not_found" in str(e) or "does not exist" in str(e):
            logger.info("[agent] SIP participant already disconnected (caller hung up first)")
        else:
            logger.error("[agent] Failed to disconnect SIP participant: %s", str(e))
    finally:
        await lk.aclose()

    # Shut down the agent's room context to trigger session close.
    # Without this, the agent stays in the room after the SIP participant
    # is removed, the session never closes, and the post-call pipeline
    # (transcript, recording path, lead creation, notifications) never runs.
    try:
        ctx = deps.get("ctx")
        if ctx:
            ctx.shutdown()
    except Exception:
        pass


def create_end_call_tool(deps: dict):
    @function_tool(
        name="end_call",
        description=(
            "Disconnect the phone line. "
            "IMPORTANT: You must have ALREADY spoken your complete farewell BEFORE calling this. "
            "Do NOT say goodbye and call this tool at the same time — finish speaking first, "
            "then call this tool separately with no additional speech. "
            "Always capture caller information before ending if no booking was made."
        ),
    )
    async def end_call(context: RunContext) -> str:
        deps["call_end_reason"][0] = "agent_ended"
        asyncio.create_task(_delayed_disconnect(deps))
        return "[Call disconnected — do not produce any further speech.]"

    return end_call
