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


async def test_a_note_to_itself_in_brackets_is_never_spoken():
    """The spoken take of 2026-11-25, word for word."""
    reply = (
        "Thank you, Leroy, but two in the afternoon tomorrow is not available. Would you like me to book "
        "your cleaning at twelve noon, or one in the afternoon instead?\n"
        "(If Leroy responds with either time, I will proceed accordingly.)"
    )
    text, _ = await _spoken(_text(reply))
    assert text.endswith("or one in the afternoon instead?") and "Leroy responds" not in text
    # The same note on the same line, in square brackets, and split across chunks.
    text, _ = await _spoken(_text("Shall I book it? (If the caller agrees, "), _text("I will book it.)"))
    assert text == "Shall I book it? "
    text, _ = await _spoken(_text("Shall I book it? [waits for the caller]"))
    assert text == "Shall I book it? "
    text, _ = await _spoken(_text("Shall I book it? (a note that never closes"))
    assert text == "Shall I book it? "


async def test_a_second_paragraph_is_not_spoken():
    reply = (
        "Thursday at two is not available. I can offer twelve noon or one in the afternoon.\n"
        "Which time would you prefer for your cleaning appointment on Thursday, Leroy?"
    )
    text, _ = await _spoken(_text(reply))
    assert text == "Thursday at two is not available. I can offer twelve noon or one in the afternoon."
    text, _ = await _spoken(_text("One line. "), _text("Still the same line.\n"), _text("Another paragraph."))
    assert text == "One line. Still the same line."
    # A line break before any speech is not the end of the reply.
    text, _ = await _spoken(_text("\nCertainly. What time would you like?"))
    assert text.strip() == "Certainly. What time would you like?"


async def test_a_tool_call_after_a_cut_reply_still_goes_through():
    tool = _tool("end_call")
    _, out = await _spoken(_text("Goodbye.\n(end of call)"), tool)
    assert out[-1] is tool


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
    assert src.count("speak_or_act(strip_tool_preamble(") == 2


# ── one reply per caller turn (preamble.speak_or_act) ───────────────────────


async def _through_speak_or_act(*chunks):
    from src.agents.appointments.preamble import speak_or_act, strip_tool_preamble

    async def _stream():
        for chunk in chunks:
            yield chunk

    spoken, calls = "", []
    async for chunk in speak_or_act(strip_tool_preamble(_stream(), {}), {"call_id": "c"}):
        spoken += chunk if isinstance(chunk, str) else (chunk.delta.content or "")
        if not isinstance(chunk, str):
            calls += [c.name for c in chunk.delta.tool_calls]
    return spoken, calls


async def test_a_reply_that_has_spoken_does_not_also_call_a_tool():
    """Spoken takes of 2026-11-25: the question went out, the tool call that
    came with it made the agent answer again without the caller speaking."""
    spoken, calls = await _through_speak_or_act(
        _text("Certainly, Leroy. "), _text("What time would you like tomorrow for your cleaning?"), _tool("check_slot"),
    )
    assert spoken == "Certainly, Leroy. What time would you like tomorrow for your cleaning?" and calls == []
    spoken, calls = await _through_speak_or_act(
        _text("Two in the afternoon is not available tomorrow. Twelve noon and one are free. Which would you prefer?"),
        _tool("check_slot"),
    )
    assert spoken.endswith("Which would you prefer?") and calls == []


async def test_a_silent_tool_call_and_the_goodbye_hang_up_are_untouched():
    assert await _through_speak_or_act(_tool("check_slot")) == ("", ["check_slot"])
    # A short lead-in is still the one that is dropped, and the tool runs.
    assert await _through_speak_or_act(_text("Let me check."), _tool("check_slot")) == ("", ["check_slot"])
    spoken, calls = await _through_speak_or_act(
        _text("Thank you for calling. "), _text("Have a lovely day. Goodbye."), _tool("end_call"),
    )
    assert spoken == "Thank you for calling. Have a lovely day. Goodbye." and calls == ["end_call"]
