"""Phase 61.3-amend: semantic regression test for the residual-audio race
that produced a false-negative in `_attempt_tool_result_replay` and let
call AJ_b8ACLgXZ4XZA (2026-05-07) cascade unrecovered.

Pre-amend predicate:
    stall_confirmed = last_frame_ms is None or last_frame_ms <= mute_set_at_ms

Post-amend predicate:
    GRACE_MS = 250
    audio_quiescent = last_frame_ms is None or last_frame_ms <= mute_set_at_ms + GRACE_MS
    stall_confirmed = (not saw_fresh_speaking) and audio_quiescent

The pre-amend check fired False on `last_frame_ms == mute_set_at_ms + 15ms`
(filler residue), silently skipping recovery. The post-amend check uses
the agent_state speak-transition flag (`saw_fresh_speaking`) as the truth
source AND the audio-frame check as belt-and-braces with a 250ms grace.

These tests bypass the full session and call `_attempt_tool_result_replay`
directly so the predicate logic is exercised in isolation.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.tools._availability_lib import _attempt_tool_result_replay


def _build_replay_session():
    """Construct the minimal session shape the recovery accesses:
    session._activity.realtime_llm_session.{chat_ctx, update_chat_ctx}.
    Returns (session, rt_session) so tests can assert on the inner mock.
    """
    rt_session = SimpleNamespace()
    rt_session.chat_ctx = SimpleNamespace(copy=MagicMock())
    chat_ctx_copy = SimpleNamespace(items=[])
    rt_session.chat_ctx.copy.return_value = chat_ctx_copy
    rt_session.update_chat_ctx = AsyncMock(return_value=None)

    activity = SimpleNamespace(realtime_llm_session=rt_session)
    session = SimpleNamespace(_activity=activity)
    return session, rt_session, chat_ctx_copy


def _build_replay_deps(diag_record, *, last_frame_ms_offset_ms: int | None):
    """deps dict with all preconditions met (state, call_id, name) and
    optionally a `last_audio_frame_at` set to `mute_set_at_ms + offset`.
    """
    if last_frame_ms_offset_ms is not None:
        diag_record[0]["last_audio_frame_at"] = 1_000_000 + last_frame_ms_offset_ms
    return {
        "_diag_record": diag_record,
        "_last_tool_state": "STATE:slot_ok token=slot_test speech=Friday at 3 PM | DIRECTIVE:offer the time, ask to book.",
        "_last_tool_call_id": "fc_test_call_id",
        "_last_tool_name": "check_slot",
        "_tool_mute_id": 1,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Regression test — the actual call AJ_b8ACLgXZ4XZA pattern
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_residual_audio_after_mute_does_not_block_recovery(
    mock_diag_record,
):
    """Filler frame stamped 15ms AFTER mute_set_at_ms must NOT block
    replay when the agent never freshly transitioned to speaking.

    This is the exact race from call AJ_b8ACLgXZ4XZA (2026-05-07):
    - mute_set_at_ms = 1_000_000
    - last_audio_frame_at = 1_000_015 (residual frame from "let me check…" filler)
    - saw_fresh_speaking = False (Gemini stalled, no fresh speak after mute)

    Pre-amend behavior: stall_confirmed=False → recovery silently skipped.
    Post-amend behavior: stall_confirmed=True (saw_fresh_speaking=False AND
    last_frame within 250ms grace) → recovery fires.
    """
    deps = _build_replay_deps(mock_diag_record, last_frame_ms_offset_ms=15)
    session, rt_session, chat_ctx_copy = _build_replay_session()

    await _attempt_tool_result_replay(
        deps=deps,
        session=session,
        mute_set_at_ms=1_000_000,
        saw_fresh_speaking=False,
    )

    rt_session.update_chat_ctx.assert_awaited_once()
    assert len(chat_ctx_copy.items) == 1
    synthetic = chat_ctx_copy.items[0]
    assert synthetic.call_id == "fc_test_call_id"
    assert synthetic.name == "check_slot"
    assert synthetic.output.startswith("STATE:slot_ok")
    assert synthetic.is_error is False
    # D-08 telemetry counter incremented.
    assert mock_diag_record[0].get("stalled_generation_recoveries") == 1


@pytest.mark.asyncio
async def test_no_audio_at_all_with_fresh_speaking_false_fires_recovery(
    mock_diag_record,
):
    """The clean-stall case: no audio frames since session start AND
    no fresh speak — both quiescence signals agree, recovery fires.
    """
    deps = _build_replay_deps(mock_diag_record, last_frame_ms_offset_ms=None)
    session, rt_session, _chat_ctx_copy = _build_replay_session()

    await _attempt_tool_result_replay(
        deps=deps,
        session=session,
        mute_set_at_ms=1_000_000,
        saw_fresh_speaking=False,
    )

    rt_session.update_chat_ctx.assert_awaited_once()


@pytest.mark.asyncio
async def test_fresh_speaking_after_mute_blocks_recovery(mock_diag_record):
    """If the agent freshly transitioned listening→speaking AFTER mute,
    Gemini IS responding to the tool result. This is not the cascade
    failure mode — recovery must NOT fire even if audio frames also
    advanced (which they will, because Gemini is speaking).
    """
    # Frame stamped well after mute — Gemini is mid-response.
    deps = _build_replay_deps(mock_diag_record, last_frame_ms_offset_ms=2_000)
    session, rt_session, _chat_ctx_copy = _build_replay_session()

    await _attempt_tool_result_replay(
        deps=deps,
        session=session,
        mute_set_at_ms=1_000_000,
        saw_fresh_speaking=True,
    )

    rt_session.update_chat_ctx.assert_not_awaited()
    assert "stalled_generation_recoveries" not in mock_diag_record[0]


@pytest.mark.asyncio
async def test_audio_well_past_grace_blocks_recovery_even_without_fresh_speak(
    mock_diag_record,
):
    """Belt-and-braces: if audio frames advanced WELL past the 250ms grace
    (e.g. 2s after mute) we treat that as Gemini speaking even if the
    state-change listener somehow missed the speak transition. Both
    quiescence signals must agree to confirm a stall.
    """
    deps = _build_replay_deps(mock_diag_record, last_frame_ms_offset_ms=2_000)
    session, rt_session, _chat_ctx_copy = _build_replay_session()

    await _attempt_tool_result_replay(
        deps=deps,
        session=session,
        mute_set_at_ms=1_000_000,
        saw_fresh_speaking=False,
    )

    rt_session.update_chat_ctx.assert_not_awaited()


@pytest.mark.asyncio
async def test_grace_boundary_inclusive_of_250ms(mock_diag_record):
    """Frame stamped exactly at the grace boundary (mute + 250ms) must
    still be treated as residue and allow recovery.
    """
    deps = _build_replay_deps(mock_diag_record, last_frame_ms_offset_ms=250)
    session, rt_session, _chat_ctx_copy = _build_replay_session()

    await _attempt_tool_result_replay(
        deps=deps,
        session=session,
        mute_set_at_ms=1_000_000,
        saw_fresh_speaking=False,
    )

    rt_session.update_chat_ctx.assert_awaited_once()


@pytest.mark.asyncio
async def test_just_past_grace_blocks_recovery(mock_diag_record):
    """One ms past the grace — treat as fresh audio, block recovery."""
    deps = _build_replay_deps(mock_diag_record, last_frame_ms_offset_ms=251)
    session, rt_session, _chat_ctx_copy = _build_replay_session()

    await _attempt_tool_result_replay(
        deps=deps,
        session=session,
        mute_set_at_ms=1_000_000,
        saw_fresh_speaking=False,
    )

    rt_session.update_chat_ctx.assert_not_awaited()
