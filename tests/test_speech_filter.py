"""Tool names never reach the agent's speech (src/agents/appointments/speech_filter.py).

A production call on 2026-11-24 ended with the agent saying "Goodbye and take
care. (functions.end_call)" and the line staying open.
"""

from types import SimpleNamespace
from unittest.mock import patch

from src.agents.appointments import speech_filter
from src.agents.appointments.speech_filter import strip_tool_mentions


def _text(content):
    return SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=[]), usage=None)


def _tool(name):
    return SimpleNamespace(delta=SimpleNamespace(content="", tool_calls=[SimpleNamespace(name=name)]), usage=None)


async def _spoken(*chunks, deps=None):
    async def _stream():
        for chunk in chunks:
            yield chunk

    out = []
    async for chunk in strip_tool_mentions(_stream(), deps):
        out.append(chunk)
    text = "".join(
        c if isinstance(c, str) else (getattr(c.delta, "content", None) or "") for c in out
    )
    return text, out


async def test_the_production_line_loses_the_tool_name():
    text, _ = await _spoken(_text("Goodbye and take care. (functions.end_call)"))
    assert text == "Goodbye and take care. "


async def test_a_mention_split_across_chunks_is_removed():
    text, _ = await _spoken(_text("Thanks for calling. Goodbye. (fun"), _text("ctions.end"), _text("_call)"))
    assert text == "Thanks for calling. Goodbye. "


async def test_every_tool_name_and_spelling_is_removed():
    for mention in ("(end_call)", "(book_appointment)", "( check_slot )", "(functions.find_appointment)",
                    "(reschedule_appointment())", "(check_day)", "(next_available_days)", "(cancel_appointment)"):
        text, _ = await _spoken(_text(f"One moment {mention} thank you."))
        assert text == "One moment  thank you.", mention


async def test_ordinary_brackets_are_spoken():
    for line in ("We open at eight (nine on Fridays).", "That's two (2) o'clock.", "Call us (any time) on Monday.",
                 "A bracket that never closes (like this"):
        text, _ = await _spoken(_text(line))
        assert text == line


async def test_tool_calls_and_plain_text_pass_through_untouched():
    tool = _tool("book_appointment")
    plain = _text("You're booked for Monday at nine.")
    text, out = await _spoken(plain, tool)
    assert out == [plain, tool] and text == "You're booked for Monday at nine."


async def test_writing_end_call_without_calling_it_ends_the_call():
    deps = {"room_name": "room-1", "call_id": "room-1", "call_end_reason": ["caller_hangup"]}
    with patch.object(speech_filter, "create_background_task") as spawn:
        await _spoken(_text("Goodbye. (end_call)"), deps=deps)
    assert spawn.call_count == 1
    spawn.call_args.args[0].close()  # the coroutine was never awaited in this test


async def test_a_real_end_call_or_another_tools_name_does_not_end_the_call_twice():
    deps = {"room_name": "room-1", "call_id": "room-1", "call_end_reason": ["caller_hangup"]}
    with patch.object(speech_filter, "create_background_task") as spawn:
        await _spoken(_text("Goodbye. (end_call)"), _tool("end_call"), deps=deps)
        await _spoken(_text("Let me see (check_slot)"), deps=deps)
        await _spoken(_text("Goodbye."), deps=deps)
    assert spawn.call_count == 0


def test_the_agent_filters_every_reply():
    from pathlib import Path
    src = (Path(__file__).parent.parent / "src" / "agents" / "appointments" / "entry.py").read_text(encoding="utf-8")
    assert src.count("strip_tool_mentions(") == 2
    assert "strip_tool_preamble(guarded_llm_stream(self._deps, _run), self._deps)" in src
