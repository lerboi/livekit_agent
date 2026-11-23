"""Keep tool names out of the agent's speech.

The prompts show tool use in examples as ``(check_slot)`` or ``(end_call)``.
The model sometimes writes that notation into its reply instead of calling the
tool. On a production call on 2026-11-24 it said "Goodbye and take care.
(functions.end_call)": the caller heard the tool's name read aloud, and the
line stayed open because no tool was called.

``strip_tool_mentions`` wraps the reply stream and removes a parenthesised tool
name from the text before it reaches text-to-speech: ``(end_call)``,
``(functions.end_call)``, ``(book_appointment())``. Any other parenthesis is
left alone. If the removed mention was ``end_call`` and the response did not
really call it, the call is ended after the goodbye has played, because that
is what the model was trying to do.
"""

from __future__ import annotations

import asyncio
import logging
import re

from ...lib.background import create_background_task
from ...tools.end_call import _delayed_disconnect
from .preamble import _parts

logger = logging.getLogger("voco-agent")

_TOOLS = (
    "check_slot|check_day|next_available_days|book_appointment|find_appointment"
    "|reschedule_appointment|cancel_appointment|end_call"
)
_MENTION_RE = re.compile(rf"\(\s*(?:functions\.)?(?:{_TOOLS})\s*(?:\(\s*\))?\s*\)")
# A parenthesis longer than this is ordinary text, not a tool mention.
MAX_HELD_CHARS = 48


def _set_text(chunk, text: str):
    """The chunk with its text replaced (a plain string, or a ChatChunk)."""
    if isinstance(chunk, str):
        return text
    delta = getattr(chunk, "delta", None)
    if delta is not None:
        try:
            delta.content = text or None
        except Exception:  # noqa: BLE001
            pass
    return chunk


async def _hang_up_after_goodbye(deps: dict) -> None:
    # Let the goodbye in this response reach the speaker queue first.
    await asyncio.sleep(0.8)
    try:
        deps["call_end_reason"][0] = "agent_ended"
    except Exception:  # noqa: BLE001
        pass
    await _delayed_disconnect(deps)


async def strip_tool_mentions(stream, deps: dict | None = None):
    held = ""           # text since an unclosed "("
    wrote_end_call = False
    called_end_call = False
    async for chunk in stream:
        part, tool_names = _parts(chunk)
        if "end_call" in tool_names:
            called_end_call = True
        if not part:
            yield chunk
            continue
        out = ""
        for ch in part:
            if held:
                held += ch
                if ch == ")" and held.endswith("()") and len(held) > 2:
                    continue  # the inner "()" of "(end_call())": the mention closes on the next ")"
                if ch == ")":
                    if _MENTION_RE.fullmatch(held):
                        if "end_call" in held:
                            wrote_end_call = True
                        logger.info("[appointments] removed a tool name from speech: %r", held)
                    else:
                        out += held
                    held = ""
                elif len(held) > MAX_HELD_CHARS:
                    out += held
                    held = ""
            elif ch == "(":
                held = ch
            else:
                out += ch
        if out == part:
            yield chunk
        elif out or not isinstance(chunk, str):
            yield _set_text(chunk, out)
    if held:
        yield held  # an unclosed parenthesis at the end of the reply: ordinary text
    if wrote_end_call and not called_end_call and deps is not None and deps.get("room_name"):
        logger.warning(
            "[appointments] the reply wrote end_call instead of calling it; ending the call. call=%s",
            deps.get("call_id"),
        )
        create_background_task(_hang_up_after_goodbye(deps), name="end-call-from-text")
