"""Phase 60.2 Fix H — deterministic pre-tool filler audio tests.

These tests assert that the 4 scoped tools (check_availability,
book_appointment, capture_lead, transfer_call) play a filler phrase
via context.session.say() as their first await.

Wave 0 scaffold: these tests are expected to FAIL until Plan 03 lands
Fix H. Plan 03's executor will:
  - Extend `_minimal_deps()` per-tool with any additional keys each
    tool needs to reach the filler call without crashing earlier.
  - Finalise the `_invoke()` pattern so the @function_tool-decorated
    inner coroutine is actually invoked (livekit-agents 1.5.1 exposes
    it via a callable — verify during Plan 03).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.tools.check_availability import create_check_availability_tool
from src.tools.book_appointment import create_book_appointment_tool
from src.tools.capture_lead import create_capture_lead_tool
from src.tools.transfer_call import create_transfer_call_tool

CHECK_AVAILABILITY_PHRASES = {
    "Let me pull up the calendar for you real quick — one moment.",
    "Give me just a second to look at what we have open that day.",
    "Let me take a look at the schedule for you — one sec.",
}
BOOK_APPOINTMENT_PHRASES = {
    "Alright, let me go ahead and lock that in for you now.",
    "Let me get that booked in for you — give me just a second.",
    "Perfect, booking that slot now — one moment.",
}
CAPTURE_LEAD_PHRASES = {
    "Let me make a note of your details so the team can follow up.",
    "Let me get all that saved down for you — one second.",
}
TRANSFER_CALL_PHRASES = {
    "Let me get you through to someone on the team — one moment.",
    "Connecting you over now, just a second.",
}


def _minimal_deps() -> dict:
    """Build a deps dict with the minimum keys each tool expects.

    Plan 03 (Fix H) inserts the filler await as the first statement of each
    tool's inner coroutine, BEFORE any early-return guards. We populate
    `supabase` (touched before `tenant_id` / `owner_phone` guards) so every
    tool reaches the filler, then exits via its own early-return path with
    a STATE+DIRECTIVE string — no real DB/network access.
    """
    return {
        "supabase": MagicMock(),
        # Required by transfer_call tool body AFTER the filler await.
        "call_end_reason": [None],
    }


# Minimum kwargs each tool needs to clear Python's signature-validation step.
# Tools return early via their own `if not tenant_id / slot_start / ...` guards;
# we just need to satisfy required positional args so the body runs.
_BOOK_APPOINTMENT_MIN_KWARGS = {
    "slot_start": "",
    "slot_end": "",
    "street_name": "",
    "postal_code": "",
    "caller_name": "",
}
_CAPTURE_LEAD_MIN_KWARGS = {
    "caller_name": "",
}


async def _invoke(tool_factory, mock_ctx, deps, **kwargs):
    """Helper: build tool, invoke it, return the string result."""
    tool = tool_factory(deps)
    # function_tool decorates; the inner coroutine is accessible via .fnc or
    # by calling the tool callable directly — verify against livekit-agents 1.5.1.
    # Plan 03 executor: finalise the invocation pattern once the real tools
    # are edited; until then this will raise AttributeError and tests fail.
    return await tool(mock_ctx, **kwargs)


# ---- per-tool: plays filler ----

@pytest.mark.asyncio
async def test_check_availability_plays_filler(mock_run_context, deps_factory):
    deps = _minimal_deps()
    await _invoke(create_check_availability_tool, mock_run_context, deps)
    assert mock_run_context.session.say.await_count >= 1
    spoken = mock_run_context.session.say.await_args.args[0]
    assert spoken in CHECK_AVAILABILITY_PHRASES


@pytest.mark.asyncio
async def test_book_appointment_plays_filler(mock_run_context, deps_factory):
    deps = _minimal_deps()
    await _invoke(
        create_book_appointment_tool,
        mock_run_context,
        deps,
        **_BOOK_APPOINTMENT_MIN_KWARGS,
    )
    assert mock_run_context.session.say.await_count >= 1
    spoken = mock_run_context.session.say.await_args.args[0]
    assert spoken in BOOK_APPOINTMENT_PHRASES


@pytest.mark.asyncio
async def test_capture_lead_plays_filler(mock_run_context, deps_factory):
    deps = _minimal_deps()
    await _invoke(
        create_capture_lead_tool,
        mock_run_context,
        deps,
        **_CAPTURE_LEAD_MIN_KWARGS,
    )
    assert mock_run_context.session.say.await_count >= 1
    spoken = mock_run_context.session.say.await_args.args[0]
    assert spoken in CAPTURE_LEAD_PHRASES


@pytest.mark.asyncio
async def test_transfer_call_plays_filler(mock_run_context, deps_factory):
    deps = _minimal_deps()
    await _invoke(create_transfer_call_tool, mock_run_context, deps)
    assert mock_run_context.session.say.await_count >= 1
    spoken = mock_run_context.session.say.await_args.args[0]
    assert spoken in TRANSFER_CALL_PHRASES


# ---- graceful degradation ----

@pytest.mark.asyncio
async def test_filler_exception_does_not_abort_tool(mock_run_context, deps_factory):
    mock_run_context.session.say = AsyncMock(side_effect=RuntimeError("boom"))
    deps = _minimal_deps()
    result = await _invoke(create_check_availability_tool, mock_run_context, deps)
    assert isinstance(result, str)
    assert len(result) > 0  # tool returned its STATE+DIRECTIVE


# ---- per-session counter isolation ----

@pytest.mark.asyncio
async def test_counter_per_session(mock_run_context, deps_factory):
    deps_a = _minimal_deps()
    deps_b = _minimal_deps()
    await _invoke(create_check_availability_tool, mock_run_context, deps_a)
    first_a = mock_run_context.session.say.await_args.args[0]
    mock_run_context.session.say.reset_mock()
    await _invoke(create_check_availability_tool, mock_run_context, deps_b)
    first_b = mock_run_context.session.say.await_args.args[0]
    # Both sessions start at idx=0, so both should get the same first phrase.
    assert first_a == first_b


# ---- rotation ----

@pytest.mark.asyncio
async def test_counter_rotation(mock_run_context, deps_factory):
    deps = _minimal_deps()
    spoken = []
    for _ in range(6):
        await _invoke(create_check_availability_tool, mock_run_context, deps)
        spoken.append(mock_run_context.session.say.await_args.args[0])
    # 3 phrases, rotated twice -> exactly 3 distinct values observed.
    assert len(set(spoken)) == 3
