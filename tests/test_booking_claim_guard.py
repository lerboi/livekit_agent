"""Booking-claim guard: the agent must never tell a caller the appointment is
booked when book_appointment has not run (observed with Groq Qwen on
2026-11-23). See src/lib/booking_claim_guard.py."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.lib import booking_claim_guard as guard
from src.lib.booking_claim_guard import (
    FORCED_TOOL_CHOICE,
    claims_booking,
    guard_active,
    guarded_llm_stream,
)

AGENT_SRC = (Path(__file__).parent.parent / "src" / "agent.py").read_text(encoding="utf-8")


def _text(content):
    return SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=[]))


def _tool_call():
    return SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=[object()]))


def _deps(**overrides):
    deps = {
        "agent_profile": "scheduling",
        "call_id": "call-1",
        "_slot_tokens": {"slot_a": {}},
        "_booking_succeeded": False,
    }
    deps.update(overrides)
    return deps


class _Runs:
    """Scripted LLM generations; records the tool choice each run was given."""

    def __init__(self, *generations):
        self.generations = list(generations)
        self.tool_choices = []
        self.closed = []

    def __call__(self, tool_choice):
        self.tool_choices.append(tool_choice)
        chunks = self.generations[len(self.tool_choices) - 1]
        index = len(self.tool_choices) - 1
        runs = self

        async def _stream():
            try:
                for chunk in chunks:
                    yield chunk
            finally:
                runs.closed.append(index)

        return _stream()


async def _collect(deps, runs):
    return [chunk async for chunk in guarded_llm_stream(deps, runs)]


# ── what counts as a claim ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "You're booked for tomorrow, Tuesday November twenty-fourth, at twelve noon.",
        "You’re all set for Thursday at three.",
        "You are confirmed for Friday at ten.",
        "It's booked. Anything else?",
        "That's confirmed for tomorrow at noon.",
        "I've booked you in for two thirty.",
        "Great, I have scheduled that for you.",
        "Your appointment is booked for Monday at nine.",
        "The demo is now confirmed.",
        "That has been booked for you.",
        "All set, Alex.",
    ],
)
def test_statements_of_a_finished_booking_are_claims(text):
    assert claims_booking(text)


@pytest.mark.parametrize(
    "text",
    [
        "So that's Alex Tan, tomorrow at twelve noon for a product demo. Shall I book it?",
        "Would you like me to book that?",
        "I can get that booked for you. What name should I use?",
        "For tomorrow afternoon, I have twelve noon or four o'clock.",
        "Happy to help. What day works best for you?",
        "Sorry, that time was just taken. I have four o'clock instead.",
        "Are you all set?",
        "So that's confirmed for noon?",
        "",
    ],
)
def test_questions_and_offers_are_not_claims(text):
    assert not claims_booking(text)


# ── when the guard watches a turn ───────────────────────────────────────────


def test_guard_watches_only_between_offer_and_booking():
    assert guard_active(_deps())
    assert not guard_active(_deps(_slot_tokens={}))  # nothing offered yet
    assert not guard_active(_deps(_booking_succeeded=True))  # a real booking exists
    assert not guard_active(_deps(agent_profile="home_service"))
    assert not guard_active(_deps(_claim_guard_trips=guard.MAX_TRIPS))


def test_guard_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(guard, "GUARD_ENABLED", False)
    assert not guard_active(_deps())


# ── stream behaviour ────────────────────────────────────────────────────────


async def test_false_claim_is_dropped_and_the_tool_is_forced():
    claim = [_text("You're"), _text(" booked for tomorrow"), _text(" at twelve noon.")]
    forced = [_tool_call()]
    runs = _Runs(claim, forced)
    deps = _deps()
    out = await _collect(deps, runs)
    assert out == forced, "none of the false claim may reach the caller"
    assert runs.tool_choices == [None, FORCED_TOOL_CHOICE]
    assert FORCED_TOOL_CHOICE["function"]["name"] == "book_appointment"
    assert deps["_claim_guard_trips"] == 1
    assert 0 in runs.closed, "the dropped generation must be closed"


async def test_reply_that_calls_the_tool_passes_untouched():
    chunks = [_tool_call()]
    runs = _Runs(chunks)
    assert await _collect(_deps(), runs) == chunks
    assert runs.tool_choices == [None]


async def test_ordinary_reply_passes_in_order():
    chunks = [_text("So that's Alex Tan, "), _text("tomorrow at twelve noon. "), _text("Shall I book it?")]
    runs = _Runs(chunks)
    deps = _deps()
    assert await _collect(deps, runs) == chunks
    assert runs.tool_choices == [None]
    assert "_claim_guard_trips" not in deps


async def test_long_reply_is_released_after_the_window():
    chunks = [_text("x" * 40), _text("y" * 60), _text(" You're booked.")]
    runs = _Runs(chunks)
    # Only the opening of a reply is examined; later text streams unchecked.
    assert await _collect(_deps(), runs) == chunks
    assert runs.tool_choices == [None]


async def test_plain_string_chunks_are_supported():
    runs = _Runs(["You're booked for noon."], [_tool_call()])
    out = await _collect(_deps(), runs)
    assert len(out) == 1 and runs.tool_choices == [None, FORCED_TOOL_CHOICE]


async def test_inactive_guard_streams_straight_through():
    claim = [_text("You're booked for tomorrow.")]
    runs = _Runs(claim)
    # After a real booking the same sentence is the truth.
    assert await _collect(_deps(_booking_succeeded=True), runs) == claim
    assert runs.tool_choices == [None]


async def test_guard_cannot_loop():
    claim = [_text("You're booked for tomorrow.")]
    deps = _deps()
    for _ in range(guard.MAX_TRIPS):
        await _collect(deps, _Runs(claim, [_tool_call()]))
    runs = _Runs(claim)
    assert await _collect(deps, runs) == claim
    assert runs.tool_choices == [None]


# ── wiring ──────────────────────────────────────────────────────────────────


def test_agent_llm_node_uses_the_guard():
    assert "async for chunk in guarded_llm_stream(self._deps, _run):" in AGENT_SRC
    assert "agent = VocoAgent(instructions=system_prompt, tools=tools, deps=deps)" in AGENT_SRC
    assert "Agent.default.llm_node(self, chat_ctx, tools, settings)" in AGENT_SRC
