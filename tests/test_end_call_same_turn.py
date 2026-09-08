"""end_call (2026-09-09): goodbye + end_call in the SAME turn.

The tool awaits RunContext.wait_for_playout() — which in livekit-agents 1.8
resolves when the assistant speech spoken right before this tool call has
finished playing (not the whole turn, so it cannot wait on itself) — and only
then schedules the disconnect. It returns None so the SDK generates no
follow-up reply (reply_required = output is not None).

Contract under test:
1. With a real RunContext, wait_for_playout is awaited BEFORE
   _delayed_disconnect is scheduled.
2. A stalled playout is bounded by GOODBYE_PLAYOUT_TIMEOUT_S and the
   disconnect still proceeds.
3. Fake contexts (SimpleNamespace / MagicMock, as the other tool tests use)
   skip the wait entirely and never raise.
4. The tool description teaches the same-turn contract (and no longer the
   two-turn one).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from livekit.agents import RunContext

from src.tools import end_call as ec


def _real_ctx_with(wait_coro_factory) -> RunContext:
    """A RunContext instance without running its __init__ (which needs a live
    AgentSession); only isinstance() and wait_for_playout are exercised."""
    ctx = RunContext.__new__(RunContext)
    ctx.wait_for_playout = wait_coro_factory  # type: ignore[method-assign]
    return ctx


def _tool_impl(deps):
    tool = ec.create_end_call_tool(deps)
    return getattr(tool, "fnc", None) or getattr(tool, "__wrapped__", None) or tool


@pytest.mark.asyncio
async def test_end_call_waits_for_goodbye_before_scheduling_disconnect(mock_deps_with_diag):
    order: list[str] = []

    async def _playout():
        await asyncio.sleep(0.01)
        order.append("playout_done")

    async def _fake_disconnect(deps):
        order.append("disconnect_scheduled")

    ctx = _real_ctx_with(_playout)
    with patch.object(ec, "_delayed_disconnect", new=_fake_disconnect):
        result = await _tool_impl(mock_deps_with_diag)(ctx)
        # the disconnect is a background task; let it run
        await asyncio.sleep(0.02)

    assert result is None
    assert order == ["playout_done", "disconnect_scheduled"]
    assert mock_deps_with_diag["call_end_reason"][0] == "agent_ended"
    assert isinstance(mock_deps_with_diag["_diag_record"][0]["end_call_invoked_at"], int)


@pytest.mark.asyncio
async def test_end_call_bounds_a_stalled_goodbye_playout(mock_deps_with_diag, monkeypatch):
    monkeypatch.setattr(ec, "GOODBYE_PLAYOUT_TIMEOUT_S", 0.02)

    async def _never_finishes():
        await asyncio.sleep(10)

    scheduled = AsyncMock()
    ctx = _real_ctx_with(_never_finishes)
    with patch.object(ec, "_delayed_disconnect", new=scheduled):
        await asyncio.wait_for(_tool_impl(mock_deps_with_diag)(ctx), timeout=1.0)
        await asyncio.sleep(0.01)
    scheduled.assert_awaited_once()


@pytest.mark.asyncio
async def test_end_call_skips_wait_for_fake_contexts(mock_deps_with_diag):
    scheduled = AsyncMock()
    with patch.object(ec, "_delayed_disconnect", new=scheduled):
        for ctx in (SimpleNamespace(), MagicMock()):
            await _tool_impl(mock_deps_with_diag)(ctx)
        await asyncio.sleep(0.01)
    assert scheduled.await_count == 2


@pytest.mark.asyncio
async def test_wait_for_goodbye_playout_swallows_errors():
    async def _boom():
        raise RuntimeError("tts died")

    await ec._wait_for_goodbye_playout(_real_ctx_with(_boom))  # must not raise


def test_end_call_description_teaches_same_turn_goodbye(mock_deps_with_diag):
    tool = ec.create_end_call_tool(mock_deps_with_diag)
    info = getattr(tool, "info", None) or getattr(tool, "_info", None)
    desc = getattr(info, "description", "") or ""
    lowered = desc.lower()
    assert "same turn" in lowered
    assert "finished playing" in lowered
    assert "separately" not in lowered
    assert "do not say goodbye and call this tool at the same time" not in lowered
