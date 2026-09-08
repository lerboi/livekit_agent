"""src/lib/tool_filler.py — runtime-owned latency cover for tool calls.

Contract under test:
- pick_filler rotates through the requested bank without repeats within a
  call, falls back generic -> still_working -> a last-resort line, and speaks
  Spanish when the caller's active language (or the tenant locale) is Spanish.
- tool_filler is a strict no-op (never raises, still yields) when the context
  is not a real livekit RunContext — this is what every existing tool test
  relies on, since they pass SimpleNamespace / MagicMock contexts.
- On a real RunContext it delegates to RunContext.with_filler with a callable
  source that returns bank lines, and the configured idle dwell.
- ThinkingSoundController only plays while armed AND the agent is thinking,
  stops on any other agent state, keeps playing through the follow-up LLM
  turn after release, and is bounded by a safety timer.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.lib import tool_filler as tf


# ── pick_filler ──────────────────────────────────────────────────────────────


def _bank(locale: str, name: str) -> set[str]:
    return set(tf._BUNDLES[locale]["tool_fillers"][name])


def test_pick_filler_rotates_without_repeats_then_falls_back():
    deps: dict = {}
    generic = _bank("en", "generic")
    seen = [tf.pick_filler(deps, "generic") for _ in range(len(generic))]
    assert set(seen) == generic
    assert len(set(seen)) == len(seen)
    # Bank exhausted -> still_working lines, then the last resort, never empty.
    extra = [tf.pick_filler(deps, "generic") for _ in range(len(_bank("en", "still_working")) + 2)]
    assert all(extra)
    assert extra[-1] == tf._LAST_RESORT["en"]


def test_pick_filler_prefers_requested_bank_then_generic():
    deps: dict = {}
    address = _bank("en", "address")
    first = tf.pick_filler(deps, "address")
    assert first in address
    rest = [tf.pick_filler(deps, "address") for _ in range(len(address) - 1)]
    assert set([first] + rest) == address
    # Next draw spills into generic, not a repeat.
    assert tf.pick_filler(deps, "address") in _bank("en", "generic")


def test_pick_filler_later_steps_use_still_working_bank():
    deps: dict = {}
    assert tf.pick_filler(deps, "booking", step=1) in _bank("en", "still_working")


@pytest.mark.parametrize(
    "deps, expected_locale",
    [
        ({}, "en"),
        ({"locale": "es"}, "es"),
        ({"locale": "en", "_active_language": "es-419"}, "es"),
        ({"locale": "es", "_active_language": "en-US"}, "en"),
    ],
)
def test_pick_filler_follows_active_language_then_tenant_locale(deps, expected_locale):
    line = tf.pick_filler(dict(deps), "generic")
    assert line in _bank(expected_locale, "generic")


# ── tool_filler: no-op outside a real RunContext ─────────────────────────────


@pytest.mark.asyncio
async def test_tool_filler_is_noop_for_fake_contexts():
    for ctx in (SimpleNamespace(), MagicMock(), None):
        deps: dict = {}
        ran = False
        async with tf.tool_filler(ctx, deps, "generic"):
            ran = True
        assert ran
        assert "_filler_used" not in deps  # nothing was spoken


@pytest.mark.asyncio
async def test_tool_filler_arms_and_releases_thinking_controller():
    ctl = MagicMock()
    deps = {"_thinking_ctl": ctl}
    async with tf.tool_filler(SimpleNamespace(), deps, "generic"):
        ctl.arm.assert_called_once()
        ctl.release.assert_not_called()
    ctl.release.assert_called_once()


@pytest.mark.asyncio
async def test_tool_filler_releases_controller_even_if_body_raises():
    ctl = MagicMock()
    deps = {"_thinking_ctl": ctl}
    with pytest.raises(RuntimeError):
        async with tf.tool_filler(SimpleNamespace(), deps, "generic"):
            raise RuntimeError("tool body failed")
    ctl.release.assert_called_once()


# ── tool_filler: delegation to RunContext.with_filler ────────────────────────


@pytest.mark.asyncio
async def test_tool_filler_delegates_to_with_filler_with_bank_callable(monkeypatch):
    calls: list[dict] = []

    class FakeRunContext:
        @asynccontextmanager
        async def with_filler(self, source, *, delay, interval=None, max_steps=None):
            calls.append({"source": source, "delay": delay, "interval": interval, "max_steps": max_steps})
            yield

    monkeypatch.setattr(tf, "_is_run_context", lambda ctx: isinstance(ctx, FakeRunContext))
    monkeypatch.setattr(tf, "TOOL_FILLER_DELAY_S", 0.42)
    deps = {"locale": "es"}
    async with tf.tool_filler(FakeRunContext(), deps, "booking", interval=6.0, max_steps=2):
        pass
    assert len(calls) == 1
    assert calls[0]["delay"] == 0.42
    assert calls[0]["interval"] == 6.0 and calls[0]["max_steps"] == 2
    # The SDK calls the source with the step index at fire time.
    assert calls[0]["source"](0) in _bank("es", "booking")
    assert calls[0]["source"](1) in _bank("es", "still_working")


@pytest.mark.asyncio
async def test_tool_filler_disabled_by_env_flag_skips_with_filler(monkeypatch):
    class FakeRunContext:
        def with_filler(self, *a, **k):  # pragma: no cover — must not be called
            raise AssertionError("with_filler must not be used when disabled")

    monkeypatch.setattr(tf, "_is_run_context", lambda ctx: True)
    monkeypatch.setattr(tf, "TOOL_FILLERS_ENABLED", False)
    async with tf.tool_filler(FakeRunContext(), {}, "generic"):
        pass


@pytest.mark.asyncio
async def test_tool_filler_survives_a_broken_with_filler(monkeypatch):
    class FakeRunContext:
        def with_filler(self, *a, **k):
            raise RuntimeError("sdk exploded")

    monkeypatch.setattr(tf, "_is_run_context", lambda ctx: True)
    ran = False
    async with tf.tool_filler(FakeRunContext(), {}, "generic"):
        ran = True
    assert ran


# ── ThinkingSoundController ──────────────────────────────────────────────────


class _FakeHandle:
    def __init__(self):
        self.stopped = False

    def done(self):
        return self.stopped

    def stop(self):
        self.stopped = True


class _FakePlayer:
    def __init__(self):
        self.plays: list[dict] = []

    def play(self, clips, *, loop=False):
        h = _FakeHandle()
        self.plays.append({"clips": clips, "loop": loop, "handle": h})
        return h


class _FakeSession:
    def __init__(self, state="listening"):
        self.agent_state = state
        self.handlers: dict[str, list] = {}

    def on(self, name, fn):
        self.handlers.setdefault(name, []).append(fn)

    def emit_state(self, new_state):
        self.agent_state = new_state
        for fn in self.handlers.get("agent_state_changed", []):
            fn(SimpleNamespace(new_state=new_state))


@pytest.mark.asyncio
async def test_thinking_sound_plays_only_while_armed_and_thinking():
    player, session = _FakePlayer(), _FakeSession("listening")
    ctl = tf.ThinkingSoundController(player, ["clip"])
    ctl.attach(session)

    # Ordinary turn: thinking without a tool -> silence.
    session.emit_state("thinking")
    assert player.plays == []
    session.emit_state("speaking")

    # Tool starts while already thinking -> typing starts immediately, looped.
    session.emit_state("thinking")
    ctl.arm()
    assert len(player.plays) == 1 and player.plays[0]["loop"] is True
    handle = player.plays[0]["handle"]

    # A filler / the reply starts speaking -> typing stops.
    session.emit_state("speaking")
    assert handle.stopped

    # Back to thinking while the tool still runs -> resumes.
    session.emit_state("thinking")
    assert len(player.plays) == 2

    # Tool released while still thinking -> keeps playing through TTFT ...
    ctl.release()
    assert not player.plays[1]["handle"].stopped
    # ... and stops as soon as the agent leaves "thinking".
    session.emit_state("listening")
    assert player.plays[1]["handle"].stopped

    # Fully released: a later thinking state (new turn, no tool) is silent.
    session.emit_state("thinking")
    assert len(player.plays) == 2


@pytest.mark.asyncio
async def test_thinking_sound_release_stops_immediately_when_not_thinking():
    player, session = _FakePlayer(), _FakeSession("thinking")
    ctl = tf.ThinkingSoundController(player, ["clip"])
    ctl.attach(session)
    ctl.arm()
    assert len(player.plays) == 1
    session.agent_state = "listening"  # state moved without an event
    ctl.release()
    assert player.plays[0]["handle"].stopped


@pytest.mark.asyncio
async def test_thinking_sound_safety_timer_bounds_playback(monkeypatch):
    monkeypatch.setattr(tf, "THINKING_SOUND_MAX_S", 0.02)
    player, session = _FakePlayer(), _FakeSession("thinking")
    ctl = tf.ThinkingSoundController(player, ["clip"])
    ctl.attach(session)
    ctl.arm()
    assert not player.plays[0]["handle"].stopped
    await asyncio.sleep(0.08)
    assert player.plays[0]["handle"].stopped


def test_thinking_sound_never_raises_on_broken_player():
    class BrokenPlayer:
        def play(self, *a, **k):
            raise RuntimeError("no track")

    session = _FakeSession("thinking")
    ctl = tf.ThinkingSoundController(BrokenPlayer(), ["clip"])
    ctl.attach(session)
    ctl.arm()  # must not raise
    session.emit_state("speaking")
    ctl.release()
