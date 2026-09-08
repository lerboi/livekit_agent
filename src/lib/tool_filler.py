"""Code-owned latency cover for tool calls (2026-09-09).

Before this module the system prompt made the LLM speak a filler sentence
before EVERY tool call ("Never emit a tool call without speaking first").
That cost a second LLM round-trip plus ~2 s of speech even when the tool was
a 50 ms cached slot lookup, so an availability answer landed ~3 s after the
caller named a time. The 2026 guidance (OpenAI realtime prompting guide, Vapi
tool-level `request-start` messages, LiveKit async tools) is the opposite:
let the model call the tool directly and have the RUNTIME cover the wait,
only when there is a wait.

Two mechanisms, both fail-open (a failure here can never break a tool):

1. `tool_filler(context, deps, bank)` — an async context manager the tools
   wrap their body in. On livekit-agents >= 1.8 it delegates to the SDK's
   `RunContext.with_filler()`: a background scheduler waits until the session
   has been idle (nobody speaking) for TOOL_FILLER_DELAY_S and only then
   speaks one short, locale-correct line via `session.say()`. Fast tools
   never trigger it; slow ones (address lookup, booking RPCs, transfer) get
   covered. Lines rotate without repeats within a call and never name a
   date, time, or slot (the prompt's anti-fabrication rule). Outside a real
   RunContext (unit tests, older SDKs) it is a no-op.

2. `ThinkingSoundController` — plays the BackgroundAudioPlayer's keyboard
   typing clip while a tool is running AND the agent is in the "thinking"
   state (it keeps playing through the follow-up LLM turn and stops the
   moment the agent starts speaking or goes back to listening). Normal
   turns with no tool call never arm it, so the caller does not hear
   typing before every sentence — only while the receptionist is "looking
   something up".

Env:
  VOCO_TOOL_FILLERS=false          disable the spoken filler (typing sound stays)
  VOCO_TOOL_FILLER_DELAY_S=0.6     idle dwell before the first filler line
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

logger = logging.getLogger("voco-agent")

TOOL_FILLERS_ENABLED = (
    os.environ.get("VOCO_TOOL_FILLERS", "true").strip().lower() != "false"
)
TOOL_FILLER_DELAY_S = float(os.environ.get("VOCO_TOOL_FILLER_DELAY_S", "0.6"))

# Bound on how long the typing sound may run after the last tool released it
# (covers the follow-up LLM TTFT; a stuck state can never loop typing forever).
THINKING_SOUND_MAX_S = float(os.environ.get("VOCO_THINKING_SOUND_MAX_S", "8"))

_MESSAGES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "messages")
_BUNDLES: dict[str, dict] = {}
for _loc in ("en", "es"):
    with open(os.path.join(_MESSAGES_DIR, f"{_loc}.json"), "r", encoding="utf-8") as _f:
        _BUNDLES[_loc] = json.load(_f)

_LAST_RESORT = {"en": "One moment.", "es": "Un momento."}


def _locale_for(deps: dict) -> str:
    """'es' when the caller is currently speaking Spanish (tracked from STT
    finals in agent.py) or the tenant defaults to Spanish; else 'en'."""
    lang = (deps.get("_active_language") or deps.get("locale") or "en").lower()
    return "es" if lang.startswith("es") else "en"


def _bank(locale: str, name: str) -> list[str]:
    try:
        return list(_BUNDLES[locale]["tool_fillers"][name])
    except (KeyError, TypeError):
        return []


def pick_filler(deps: dict, bank: str, step: int = 0) -> str:
    """Next unused filler line for this call. Step 0 draws from `bank`
    (falling back to `generic`); later steps draw from `still_working`.
    Order is shuffled once per call per bank so consecutive calls to the same
    business do not all open with the same line. Never returns an empty string."""
    locale = _locale_for(deps)
    used: set[str] = deps.setdefault("_filler_used", set())
    orders: dict[str, list[str]] = deps.setdefault("_filler_order", {})

    def _draw(name: str) -> str | None:
        key = f"{locale}:{name}"
        if key not in orders:
            lines = _bank(locale, name)
            random.shuffle(lines)
            orders[key] = lines
        for line in orders[key]:
            if line not in used:
                used.add(line)
                return line
        return None

    names = ["still_working", "generic"] if step > 0 else [bank, "generic", "still_working"]
    for name in names:
        line = _draw(name)
        if line:
            return line
    return _LAST_RESORT[locale]


class ThinkingSoundController:
    """Typing sound while a tool runs. See module docstring."""

    def __init__(self, player: Any, clips: Any) -> None:
        self._player = player
        self._clips = clips
        self._session: Any = None
        self._armed = 0
        self._handle: Any = None
        self._timer: asyncio.TimerHandle | None = None

    def attach(self, session: Any) -> None:
        self._session = session
        session.on("agent_state_changed", self._on_agent_state)

    # -- events -----------------------------------------------------------
    def _on_agent_state(self, ev: Any) -> None:
        try:
            state = getattr(ev, "new_state", None)
            if state == "thinking":
                if self._armed > 0:
                    self._start()
            else:
                # speaking / listening / initializing: the receptionist is
                # talking or waiting on the caller — never type over that.
                self._stop()
        except Exception as exc:  # noqa: BLE001 — audio polish must never raise
            logger.debug("[thinking-sound] state handler error: %s", exc)

    # -- tool lifecycle ---------------------------------------------------
    def arm(self) -> None:
        self._armed += 1
        try:
            if self._session is not None and self._session.agent_state == "thinking":
                self._start()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[thinking-sound] arm error: %s", exc)

    def release(self) -> None:
        self._armed = max(0, self._armed - 1)
        if self._armed:
            return
        # Keep typing through the follow-up LLM turn (still "thinking");
        # the next state change or the safety timer stops it.
        try:
            if self._session is None or self._session.agent_state != "thinking":
                self._stop()
        except Exception:  # noqa: BLE001
            self._stop()

    # -- playback ---------------------------------------------------------
    def _start(self) -> None:
        if self._handle is not None and not self._handle.done():
            return
        try:
            self._handle = self._player.play(self._clips, loop=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[thinking-sound] play failed: %s", exc)
            self._handle = None
            return
        self._cancel_timer()
        try:
            self._timer = asyncio.get_running_loop().call_later(
                THINKING_SOUND_MAX_S, self._stop
            )
        except RuntimeError:
            self._timer = None

    def _stop(self) -> None:
        self._cancel_timer()
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.stop()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[thinking-sound] stop failed: %s", exc)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


def _is_run_context(context: Any) -> bool:
    try:
        from livekit.agents import RunContext

        return isinstance(context, RunContext)
    except Exception:  # noqa: BLE001
        return False


@asynccontextmanager
async def tool_filler(
    context: Any,
    deps: dict,
    bank: str = "generic",
    *,
    interval: float | None = None,
    max_steps: int | None = None,
) -> AsyncIterator[None]:
    """Wrap a tool body: arms the typing sound and schedules an idle-gated
    spoken filler for the duration of the block. Never raises."""
    ctl = deps.get("_thinking_ctl")
    if ctl is not None:
        try:
            ctl.arm()
        except Exception:  # noqa: BLE001
            ctl = None

    cm = None
    if TOOL_FILLERS_ENABLED and _is_run_context(context):
        try:
            cm = context.with_filler(
                lambda step: pick_filler(deps, bank, step),
                delay=TOOL_FILLER_DELAY_S,
                interval=interval,
                max_steps=max_steps,
            )
            await cm.__aenter__()
        except Exception as exc:  # noqa: BLE001 — cover is optional, the tool is not
            logger.warning("[tool-filler] with_filler unavailable (%s); tool runs uncovered", exc)
            cm = None
    try:
        yield
    finally:
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[tool-filler] with_filler exit error: %s", exc)
        if ctl is not None:
            try:
                ctl.release()
            except Exception:  # noqa: BLE001
                pass
