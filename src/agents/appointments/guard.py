"""Stop the agent saying a booking change is done when no tool did it.

Same idea as ``src/lib/booking_claim_guard`` (which reads "you're booked"),
widened to the three writes this agent has: book, move, cancel. Seen on a
scripted call on 2026-11-23: the model answered "yes, book it" with "You're
booked for tomorrow at twelve noon" and never called the tool.

On the turns where it can matter (a time or an appointment has been put to
the caller since the last completed write) the first words of the reply are
held back. If they state that a change is done and the reply contains no tool
call, the reply is dropped before any of it is spoken and the turn is run
again:

* only a new booking is in play: ``book_appointment`` is forced, as in the
  original guard;
* an existing appointment is in play: no tool is forced, because the reply
  may have been describing that appointment ("you're booked for Thursday")
  and a forced cancel or move would be worse than the false claim. The model
  is told that nothing has changed and answers again.

The phrases matched are statements that a change is done. "You're all set"
is deliberately not one of them: it is also how people say goodbye.

A second check covers the start of the call, before any calendar tool has
returned. On a scripted call on 2026-11-24 the model answered "what times do
you have tomorrow afternoon?" with "Let me check that for you. I have you down
for tomorrow at two o'clock" and later "You're all set", having called no tool
at all. Until a tool has returned, the model knows nothing about the calendar,
so a reply in that phase is held back whole (these replies are a sentence or
two) and dropped if it names a time, announces a lookup or claims a change
without a tool call. The turn is then run again with a tool call required.

At most ``MAX_TRIPS`` times per call. ``VOCO_BOOKING_CLAIM_GUARD=false`` turns
it off. English only.
"""

from __future__ import annotations

import logging
import re
from typing import AsyncIterable, Callable

from ...lib.booking_claim_guard import (
    FORCED_TOOL_CHOICE,
    GUARD_ENABLED,
    MAX_TRIPS,
    WINDOW_CHARS,
    _chunk_parts,
)

logger = logging.getLogger("voco-agent")

NUDGE = (
    "Your last reply began by telling the caller a booking change was done, "
    "but no tool has made any change since they asked. Nothing has changed "
    "yet. If the caller agreed to a change, call the tool for it now and "
    "confirm using its result. If you were only describing an appointment "
    "that already exists, say so without implying anything new was done."
)

_APOS = "(?:'|’)"
_DONE = "booked|confirmed|scheduled|moved|rescheduled|cancell?ed|updated"
_CLAIM_RE = re.compile(
    r"\b(?:"
    rf"you(?:{_APOS}re| are)\s+(?:now\s+|all\s+)?(?:booked|confirmed|scheduled|rescheduled|moved)"
    rf"|(?:it|that)(?:{_APOS}s| is| has been)\s+(?:now\s+|all\s+)?(?:{_DONE})"
    rf"|i(?:{_APOS}ve| have)\s+(?:now\s+|just\s+|gone ahead and\s+)?(?:{_DONE}|changed)"
    rf"|(?:appointment|booking|demo|call|meeting|slot)\s+(?:is|has been)\s+(?:now\s+|all\s+)?(?:{_DONE})"
    rf"|has\s+been\s+(?:{_DONE})"
    r"|all\s+booked\b"
    r")",
    re.IGNORECASE,
)


def claims_change(text: str) -> bool:
    """True when the text states, as fact, that an appointment was booked,
    moved or cancelled. Questions and offers do not count."""
    if not text:
        return False
    match = _CLAIM_RE.search(text)
    if not match:
        return False
    sentence_end = re.search(r"[.!?]", text[match.end():])
    return not (sentence_end and sentence_end.group() == "?")


GROUNDING_NUDGE = (
    "Your last reply named a time, said you were checking, or said something "
    "was booked, but you have not called a tool. You know nothing about the "
    "calendar until a tool returns. Call the tool the caller's request needs "
    "now, silently, and answer from what it returns."
)

_NUMBER = "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
_UNGROUNDED_RE = re.compile(
    r"\b(?:"
    r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)"
    rf"|(?:{_NUMBER})\s+(?:o{_APOS}?clock|thirty|fifteen|forty[- ]five)"
    rf"|(?:{_NUMBER}|\d{{1,2}})\s+in\s+the\s+(?:morning|afternoon|evening)"
    r"|noon"
    rf"|i\s+have\s+you\s+down"
    rf"|let\s+me\s+(?:check|look|pull|see|find)"
    r"|one\s+moment|just\s+a\s+(?:moment|second|sec)|bear\s+with\s+me"
    r")\b",
    re.IGNORECASE,
)
# A question about opening hours is answered from the prompt, with times, and
# needs no tool.
_HOURS_RE = re.compile(r"\b(?:open|opening|hours|close[sd]?|closing)\b", re.IGNORECASE)

# "I can offer you Monday the thirtieth, Tuesday the first ...": days offered
# with no lookup (a production call on 2026-11-24). An offer phrase and a day
# in the same reply.
_OFFER_RE = re.compile(
    rf"\b(?:i\s+can\s+(?:offer|do)|i(?:{_APOS}ve|\s+have)\s+got|i\s+have|we\s+have"
    rf"|we(?:{_APOS}ve|\s+have)\s+got|(?:is|are)\s+(?:available|free)|availability|openings?)\b",
    re.IGNORECASE,
)
_DAY_RE = re.compile(
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|tomorrow"
    r"|january|february|march|april|june|july|august|september|october|november|december)\b",
    re.IGNORECASE,
)

# "I'll go ahead and book your product demo for Monday": a promise with no
# tool call behind it. Nothing was booked on that call.
_PROMISE_RE = re.compile(
    rf"\b(?:i(?:{_APOS}ll|\s+will|{_APOS}m\s+going\s+to|\s+am\s+going\s+to)\s+(?:go\s+ahead\s+and\s+|now\s+|just\s+)?"
    r"(?:book|schedule|move|reschedule|cancel|lock|get\s+(?:you|that|it)\s+(?:booked|moved|cancell?ed|scheduled))"
    r"|(?:booking|scheduling|moving|rescheduling|cancell?ing)\s+(?:that|it|you|your)\b)",
    re.IGNORECASE,
)

PROMISE_NUDGE = (
    "Your last reply told the caller you would make a booking change, but you "
    "called no tool, so nothing happened. If the caller has agreed and you "
    "have what the tool needs, call the tool now and confirm from its result. "
    "If something is still missing, ask for it instead of promising."
)


def promises_change(text: str) -> bool:
    """True when the text says the agent is about to book, move or cancel.
    Offers put as questions ("Shall I book it?") do not count."""
    if not text:
        return False
    match = _PROMISE_RE.search(text)
    if not match:
        return False
    sentence_end = re.search(r"[.!?]", text[match.end():])
    return not (sentence_end and sentence_end.group() == "?")


def ungrounded(text: str) -> bool:
    """True when a reply made before any tool has returned says something only
    a tool could have told it."""
    if not text:
        return False
    if claims_change(text) or promises_change(text):
        return True
    if _HOURS_RE.search(text):
        return False
    return bool(_UNGROUNDED_RE.search(text)) or bool(_OFFER_RE.search(text) and _DAY_RE.search(text))


def tool_has_returned(deps: dict) -> bool:
    return "_last_tool_state" in deps


def _latest_offer_at(deps: dict) -> float:
    stamps = [e.get("created_at", 0) for e in (deps.get("_slot_tokens") or {}).values()]
    # An appointment booked on this call is a completed write, not an offer.
    stamps += [
        e.get("created_at", 0)
        for e in (deps.get("_appt_tokens") or {}).values()
        if not e.get("own")
    ]
    return max(stamps, default=0)


def guard_active(deps: dict) -> bool:
    """Something has been offered or looked up since the last completed write."""
    if not GUARD_ENABLED or deps.get("_claim_guard_trips", 0) >= MAX_TRIPS:
        return False
    latest = _latest_offer_at(deps)
    return latest > 0 and latest > deps.get("_last_write_at", 0)


def forced_choice(deps: dict):
    """(tool_choice, nudge) for the second generation."""
    if deps.get("_appt_tokens"):
        return None, NUDGE
    return FORCED_TOOL_CHOICE, None


async def guarded_llm_stream(
    deps: dict,
    run: Callable[[object | None, str | None], AsyncIterable],
):
    """Yield the model's reply, replacing a false "it's done" with a second
    generation. ``run(tool_choice, nudge)`` starts one generation;
    ``(None, None)`` is the session's normal behaviour."""
    if GUARD_ENABLED and not tool_has_returned(deps) and deps.get("_claim_guard_trips", 0) < MAX_TRIPS:
        async for chunk in _grounded_stream(deps, run):
            yield chunk
        return

    if not guard_active(deps):
        async for chunk in run(None, None):
            yield chunk
        return

    held: list = []
    text = ""
    released = False
    tripped = False
    promised = False
    stream = run(None, None)
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
            elif claims_change(text):
                tripped = True
                break
            elif promises_change(text):
                # Keep reading: the tool call may follow the promise in the
                # same response. Decided when the stream ends.
                promised = True
                continue
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

    if promised and not released and not tripped:
        # The reply promised a change and ended without a tool call. No tool
        # is forced (the caller may not have agreed yet): the model is told
        # that nothing happened and answers again.
        deps["_claim_guard_trips"] = deps.get("_claim_guard_trips", 0) + 1
        logger.warning(
            "[claim-guard] reply promised a change with no tool call; dropped it "
            "and answering again. call=%s text=%r",
            deps.get("call_id"), text[:120],
        )
        async for chunk in run(None, PROMISE_NUDGE):
            yield chunk
        return

    if not tripped:
        for item in held:  # a short reply that ended inside the window
            yield item
        return

    deps["_claim_guard_trips"] = deps.get("_claim_guard_trips", 0) + 1
    tool_choice, nudge = forced_choice(deps)
    logger.warning(
        "[claim-guard] reply claimed a change with no tool call; dropped it (%s). "
        "call=%s text=%r",
        "forcing book_appointment" if tool_choice else "answering again with a correction",
        deps.get("call_id"), text[:120],
    )
    async for chunk in run(tool_choice, nudge):
        yield chunk


async def _grounded_stream(deps: dict, run):
    """The reply for a turn on which no calendar tool has returned yet: held
    whole, and replaced by a required tool call if it is ungrounded."""
    held: list = []
    text = ""
    released = False
    async for chunk in run(None, None):
        if released:
            yield chunk
            continue
        held.append(chunk)
        part, has_tool_call = _chunk_parts(chunk)
        text += part
        if has_tool_call:
            released = True
            for item in held:
                yield item
            held = []
    if released:
        return
    if not ungrounded(text):
        for item in held:
            yield item
        return

    deps["_claim_guard_trips"] = deps.get("_claim_guard_trips", 0) + 1
    logger.warning(
        "[claim-guard] reply spoke from the calendar before any tool returned; "
        "dropped it and requiring a tool. call=%s text=%r",
        deps.get("call_id"), text[:160],
    )
    async for chunk in run("required", GROUNDING_NUDGE):
        yield chunk
