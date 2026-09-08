"""
end_call tool -- graceful call termination.

2026-09-09 (livekit-agents 1.8): the goodbye and end_call now happen in the
SAME turn. The tool awaits `RunContext.wait_for_playout()` — which resolves
when the assistant speech spoken right before this tool call has finished
playing (NOT the whole turn, so it cannot wait on itself) — and only then
tears the line down. Before this the prompt asked the model to say goodbye,
wait for the caller to say something, and call end_call in a separate turn:
that cost an extra LLM round-trip, left an awkward silence, and if the caller
stayed quiet the line simply sat open. Returning None suppresses the SDK's
follow-up reply (reply_required = output is not None), so nothing can be
spoken over the hang-up.
"""

import asyncio
import logging
import os
import time

import sentry_sdk
from livekit import api
from livekit.agents import function_tool, RunContext

from ..lib.background import create_background_task

logger = logging.getLogger(__name__)

# Upper bound on waiting for the goodbye sentence to finish playing before the
# disconnect proceeds anyway (a stalled TTS must never hold the line open).
GOODBYE_PLAYOUT_TIMEOUT_S = float(os.environ.get("VOCO_GOODBYE_PLAYOUT_TIMEOUT_S", "20"))


async def _delayed_disconnect(deps: dict) -> None:
    """Wait for the agent's current speech to finish playing, then tear down the call.

    Uses `SpeechHandle.wait_for_playout()` via `session.current_speech` for
    deterministic waiting (the watchdog / recovery callers rely on this; the
    end_call tool itself has already awaited its own goodbye via
    `RunContext.wait_for_playout()` by the time it schedules this). Capped at
    20s as a hung-generation safety belt.
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


async def _wait_for_goodbye_playout(context) -> None:
    """Block until the goodbye spoken in this turn has played out. Never raises.

    Only a real RunContext (livekit-agents >= 1.8) exposes wait_for_playout;
    unit tests pass SimpleNamespace / MagicMock contexts and skip the wait.
    """
    if not isinstance(context, RunContext):
        return
    try:
        await asyncio.wait_for(context.wait_for_playout(), timeout=GOODBYE_PLAYOUT_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning(
            "[agent] end_call: goodbye playout wait exceeded %ss; disconnecting anyway",
            GOODBYE_PLAYOUT_TIMEOUT_S,
        )
    except Exception as e:  # noqa: BLE001 — teardown must proceed regardless
        logger.warning("[agent] end_call: goodbye playout wait error (%s); continuing", e)


def create_end_call_tool(deps: dict):
    @function_tool(
        name="end_call",
        description=(
            "Hang up the line. Call this in the SAME turn as your goodbye, right after "
            "the goodbye sentence — the line stays open until your goodbye has finished "
            "playing, then disconnects. Never call it before the goodbye is spoken. "
            "Always capture caller information before ending if no booking was made."
        ),
    )
    async def end_call(context: RunContext) -> None:
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

        # Let the goodbye spoken in this same turn finish before anything else.
        await _wait_for_goodbye_playout(context)

        # Held reference (lib/background) — a GC'd disconnect task would leave
        # the room open until the 10-min duration watchdog.
        create_background_task(_delayed_disconnect(deps))
        # None = no follow-up LLM reply for this tool call (the line is going
        # down; a generated "Goodbye!" would only ever be cut off mid-word).
        return None

    return end_call
