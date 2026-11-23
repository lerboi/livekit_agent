"""Stop the agent telling a caller "you're booked" when nothing was booked.

Observed 2026-11-23 on a scripted call (Groq Qwen, scheduling profile): after
the caller said "yes, please book it" the model answered "You're booked for
tomorrow at twelve noon" without calling book_appointment. No appointment
existed. The prompt forbids this; a prompt rule is not a guarantee.

The guard sits in the agent's LLM node. On the turns where it matters (a time
has been offered, nothing is booked yet) it holds back the first few words of
the model's reply. If they claim a booking, the reply is dropped before any of
it is spoken and the turn is re-run with the tool choice forced to
book_appointment, so the booking really happens and the confirmation the
caller hears comes from the tool's result. Every other turn streams straight
through, and a reply that starts with a tool call is released immediately.

VOCO_BOOKING_CLAIM_GUARD=false turns it off.
"""

from __future__ import annotations

import logging
import os
import re
from typing import AsyncIterable, Callable

logger = logging.getLogger("voco-agent")

GUARD_ENABLED = os.environ.get("VOCO_BOOKING_CLAIM_GUARD", "true").strip().lower() not in (
    "0", "false", "off", "no",
)

# Characters of reply text examined before the rest is streamed unchecked.
# A false confirmation opens the reply ("You're booked for ..."), so a short
# window catches it while adding only a few tokens of delay.
WINDOW_CHARS = 90
# Upper bound on forced re-runs per call, so the guard can never loop.
MAX_TRIPS = 2

FORCED_TOOL_CHOICE = {"type": "function", "function": {"name": "book_appointment"}}

# Statements that the booking is done. Questions and offers ("Shall I book
# it?", "Want me to get that booked?") must not match: forcing the tool on
# those would book before the caller agreed.
_CLAIM_RE = re.compile(
    r"\b(?:"
    r"you(?:['’]re| are)\s+(?:now\s+|all\s+)?(?:booked|confirmed|scheduled|set)"
    r"|(?:it|that)(?:['’]s| is)\s+(?:now\s+|all\s+)?(?:booked|confirmed|scheduled)"
    r"|i(?:['’]ve| have)\s+(?:now\s+|just\s+)?(?:booked|scheduled|confirmed)"
    r"|(?:appointment|booking|demo|call|meeting|slot)\s+is\s+(?:now\s+|all\s+)?(?:booked|confirmed|scheduled|set)"
    r"|has\s+been\s+(?:booked|confirmed|scheduled)"
    r"|all\s+(?:booked|set)\b"
    r")",
    re.IGNORECASE,
)


def claims_booking(text: str) -> bool:
    """True when the text states, as fact, that the appointment is booked."""
    if not text:
        return False
    match = _CLAIM_RE.search(text)
    if not match:
        return False
    # "…all set?" / "…is that confirmed?" style questions are not claims.
    sentence_end = re.search(r"[.!?]", text[match.end():])
    return not (sentence_end and sentence_end.group() == "?")


def guard_active(deps: dict) -> bool:
    """Whether this turn needs watching: scheduling profile, a time has been
    offered, and nothing has been booked on this call yet."""
    return bool(
        GUARD_ENABLED
        and deps.get("agent_profile") == "scheduling"
        and deps.get("_slot_tokens")
        and not deps.get("_booking_succeeded")
        and deps.get("_claim_guard_trips", 0) < MAX_TRIPS
    )


def _chunk_parts(chunk) -> tuple[str, bool]:
    """(text, has_tool_call) for a ChatChunk or a plain string."""
    if isinstance(chunk, str):
        return chunk, False
    delta = getattr(chunk, "delta", None)
    if delta is None:
        return "", False
    return (getattr(delta, "content", None) or ""), bool(getattr(delta, "tool_calls", None))


async def guarded_llm_stream(
    deps: dict,
    run: Callable[[object | None], AsyncIterable],
):
    """Yield the model's reply, replacing a false booking claim with a forced
    book_appointment call.

    ``run(tool_choice)`` starts one LLM generation and returns its chunk
    stream; ``None`` means the session's normal tool choice.
    """
    if not guard_active(deps):
        async for chunk in run(None):
            yield chunk
        return

    held: list = []
    text = ""
    released = False
    tripped = False
    stream = run(None)
    try:
        async for chunk in stream:
            if released:
                yield chunk
                continue
            held.append(chunk)
            part, has_tool_call = _chunk_parts(chunk)
            text += part
            if has_tool_call:
                released = True  # the model is doing the work, not claiming it
            elif claims_booking(text):
                tripped = True
                break
            elif len(text) >= WINDOW_CHARS:
                released = True
            if released:
                for item in held:
                    yield item
                held = []
    finally:
        if tripped:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # noqa: BLE001
                    pass

    if not tripped:
        for item in held:  # a short reply that ended inside the window
            yield item
        return

    deps["_claim_guard_trips"] = deps.get("_claim_guard_trips", 0) + 1
    logger.warning(
        "[claim-guard] reply claimed a booking with no book_appointment call; "
        "dropped it and forcing the tool. call=%s text=%r",
        deps.get("call_id"), text[:120],
    )
    async for chunk in run(FORCED_TOOL_CHOICE):
        yield chunk
