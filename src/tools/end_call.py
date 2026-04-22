"""
end_call tool -- graceful call termination.
Ported from src/tools/end-call.js -- same logic, same behavior.
Gemini generates the farewell, then we disconnect the SIP participant.
"""

import asyncio
import logging
import time

import sentry_sdk
from livekit import api
from livekit.agents import function_tool, RunContext

logger = logging.getLogger(__name__)


async def _delayed_disconnect(deps: dict) -> None:
    """Wait for the agent's current speech to finish playing, then tear down the call.

    Uses livekit-agents 1.5.1 native `SpeechHandle.wait_for_playout()` via
    `session.current_speech` for deterministic waiting — replaces the old fixed
    12s `asyncio.sleep()` that cut off longer farewells and fired too early on
    shorter ones. Capped at 20s as a hung-generation safety belt.
    """
    session = deps.get("session")
    try:
        current = session.current_speech if session else None
        if current:
            await asyncio.wait_for(current.wait_for_playout(), timeout=20)
        else:
            # No active speech when end_call returned — still allow a brief
            # moment for any SIP-side RTP jitter buffer to drain before the
            # hard disconnect.
            await asyncio.sleep(1)
    except asyncio.TimeoutError:
        logger.warning("[agent] end_call: playout wait exceeded 20s; disconnecting anyway")
    except Exception as e:
        logger.warning("[agent] end_call: playout wait error (%s); disconnecting anyway", e)

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
        # Phase 60.3 Stream A: capture end_call invocation timestamp on the
        # per-call diagnostic record (R-A4). diag_record is seeded in
        # agent.py entrypoint as deps["_diag_record"] = [{...}].
        now_ms = int(time.time() * 1000)
        diag = deps.get("_diag_record")
        if diag and isinstance(diag, list) and len(diag) > 0 and diag[0] is not None:
            diag[0]["end_call_invoked_at"] = now_ms
        try:
            sentry_sdk.add_breadcrumb(
                category="goodbye_race",
                message="end_call invoked",
                data={"ts_ms": now_ms, "call_id": deps.get("call_id")},
                level="info",
            )
        except Exception:
            pass  # diagnostic breadcrumb must never block tool execution

        deps["call_end_reason"][0] = "agent_ended"
        asyncio.create_task(_delayed_disconnect(deps))
        # Let any in-flight sentence finish naturally (the disconnect task
        # waits for playout). The directive only prevents Gemini from
        # starting a NEW turn after the current one completes.
        return (
            "STATE:call_ending | DIRECTIVE:the line is about to disconnect; "
            "do not start a new turn or produce further speech after your "
            "current sentence completes."
        )

    return end_call
