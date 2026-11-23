"""Keep the agent quiet in the moment before it uses a tool.

The prompt tells the model to call tools silently, and the runtime covers a
real wait (typing sound, then a spoken line only if the tool is slow). The
model does not always comply. Seen on scripted calls on 2026-11-24, in the
same response as a tool call:

* "Let me check that for you."  (about 1.3 s of speech before the answer)
* "[assistant]"                 (a stray role marker, read aloud)

``strip_tool_preamble`` wraps the reply stream. It holds the opening of a
reply until it can tell which kind of reply this is:

* a tool call follows the opening: the held text is dropped and the tool
  call goes through. ``end_call`` is the exception: the goodbye that comes
  with it is the whole point, so that text is kept.
* the reply carries on past its first sentence, or ends: it is ordinary
  speech and is released unchanged.

The hold lasts one sentence plus one token, so ordinary replies start a few
tens of milliseconds later than they would have.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("voco-agent")

# Text released regardless once this much has accumulated.
MAX_HELD_CHARS = 120
# Tools whose accompanying speech must be kept.
SPEAK_WITH = frozenset({"end_call"})

# A finished sentence followed by the start of another.
_CONTINUES_RE = re.compile(r"[.!?…][\"')\]]?\s+\S")
# A reply that is nothing but a role marker.
_ROLE_MARKER_RE = re.compile(r"^\s*[\[(<]?\s*(?:assistant|agent|ai)\s*[\])>]?\s*:?\s*$", re.IGNORECASE)


def _parts(chunk) -> tuple[str, list]:
    """(text, tool call names) for a ChatChunk or a plain string."""
    if isinstance(chunk, str):
        return chunk, []
    delta = getattr(chunk, "delta", None)
    if delta is None:
        return "", []
    calls = getattr(delta, "tool_calls", None) or []
    return (getattr(delta, "content", None) or ""), [getattr(c, "name", "") for c in calls]


def _without_text(chunk):
    """The chunk with its spoken text removed, or None if nothing is left."""
    if isinstance(chunk, str):
        return None
    delta = getattr(chunk, "delta", None)
    if delta is None:
        return chunk  # usage or other bookkeeping: keep
    if getattr(delta, "tool_calls", None):
        try:
            delta.content = None
        except Exception:  # noqa: BLE001
            pass
        return chunk
    if getattr(chunk, "usage", None) is not None:
        try:
            delta.content = None
        except Exception:  # noqa: BLE001
            pass
        return chunk
    return None


async def strip_tool_preamble(stream, deps: dict | None = None):
    held: list = []
    text = ""
    released = False
    async for chunk in stream:
        if released:
            yield chunk
            continue
        part, tool_names = _parts(chunk)
        if tool_names:
            released = True
            if any(name in SPEAK_WITH for name in tool_names):
                for item in held:
                    yield item
            else:
                if text.strip():
                    logger.info(
                        "[appointments] dropped speech before %s: %r",
                        ",".join(tool_names), text.strip()[:80],
                    )
                for item in held:
                    kept = _without_text(item)
                    if kept is not None:
                        yield kept
                kept = _without_text(chunk) if part else chunk
                if kept is not None:
                    yield kept
                held = []
                continue
            held = []
            yield chunk
            continue
        held.append(chunk)
        text += part
        if _CONTINUES_RE.search(text) or len(text) >= MAX_HELD_CHARS:
            released = True
            for item in held:
                yield item
            held = []
    if released:
        return
    if _ROLE_MARKER_RE.match(text) and text.strip():
        logger.info("[appointments] dropped a stray role marker: %r", text.strip()[:40])
        for item in held:
            kept = _without_text(item)
            if kept is not None:
                yield kept
        return
    for item in held:
        yield item


async def goodbye_with_end_call(stream, deps: dict | None = None):
    """Make sure the caller hears a goodbye before the line goes down.

    The prompt asks for the goodbye and ``end_call`` in one response. The
    model sometimes sends the tool call alone (production rehearsals and a
    spoken take on 2026-11-24: the call ended in silence after "That's all,
    thanks. Bye."). When a response calls ``end_call`` and has spoken nothing,
    the call's goodbye line (``deps["_goodbye_text"]``) is put in front of the
    tool call. ``end_call`` waits for that speech to finish before hanging up.
    A response that already says something is left alone.
    """
    spoken = ""
    done = False
    async for chunk in stream:
        if not done:
            part, tool_names = _parts(chunk)
            spoken += part
            if "end_call" in tool_names:
                done = True
                goodbye = ((deps or {}).get("_goodbye_text") or "").strip()
                if goodbye and not spoken.strip():
                    logger.info("[appointments] end_call came with no goodbye; speaking the call's goodbye line")
                    yield goodbye
        yield chunk
