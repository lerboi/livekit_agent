"""src/agent.py wiring for the 2026-09-09 call-experience changes (items 1-3).

Source-grep invariants in the same style as tests/test_greeting_not_interruptible.py
(behavioral confirmation is the live UAT call):

1. Turn handling: audio-native inference.TurnDetector by default (version
   pinned explicitly — a self-hosted worker would otherwise auto-select the
   local v1-mini), LiveKit's recommended dynamic 0.3/2.5 s endpointing, and
   adaptive interruption with a two-word gate. The deprecated text model is
   retained strictly as the VOCO_TURN_DETECTOR=text rollback.
2. Greet-first: ONE tenant+services query before the greeting, the per-caller
   context loads in the background and lands via agent.update_instructions(),
   and the caller is unmuted only after both the greeting playout and that
   loader (bounded) — never against half-built instructions.
3. Background audio: the typing sound is scoped to tool execution through
   ThinkingSoundController, never handed to the SDK as `thinking_sound=`
   (which would play it before every sentence); the player is closed on
   session close.
4. STT keyterms are on by default (Deepgram documents keyterm + multilingual
   Nova-3 support).
"""
from __future__ import annotations

import re
from pathlib import Path

_AGENT_SRC = Path(__file__).parent.parent / "src" / "agent.py"
_PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def _src() -> str:
    return _AGENT_SRC.read_text(encoding="utf-8")


# ── 1. turn handling ─────────────────────────────────────────────────────────


def test_defaults_are_audio_turn_detector_with_recommended_endpointing():
    from src import agent

    assert agent.TURN_DETECTOR_MODE == "audio"
    assert agent.TURN_DETECTOR_VERSION == "v1"
    assert agent.ENDPOINTING_MODE == "dynamic"
    assert agent.MIN_ENDPOINTING_DELAY_S == 0.3
    assert agent.MAX_ENDPOINTING_DELAY_S == 2.5
    assert agent.INTERRUPTION_MODE == "adaptive"
    assert agent.MIN_INTERRUPTION_WORDS == 2
    assert agent.PREEMPTIVE_GENERATION is True


def test_audio_detector_version_is_passed_explicitly():
    src = _src()
    assert '_td_kwargs = {"version": TURN_DETECTOR_VERSION}' in src, (
        "on a self-hosted worker the SDK auto-selects v1-mini unless the version is explicit"
    )
    assert "inference.TurnDetector(**_td_kwargs)" in src
    # confidence threshold is a live-tuning lever, SDK default unless set
    from src import agent
    assert agent.TURN_DETECTOR_UNLIKELY_THRESHOLD is None
    assert '_td_kwargs["unlikely_threshold"] = TURN_DETECTOR_UNLIKELY_THRESHOLD' in src
    # Text model kept only for rollback, selected by the env-driven mode.
    assert 'if TURN_DETECTOR_MODE == "text":' in src
    assert "turn_detection = MultilingualModel()" in src


def test_session_uses_turn_handling_dict_not_legacy_kwargs():
    src = _src()
    assert "turn_handling=turn_handling" in src
    for legacy in (
        "min_endpointing_delay=MIN_ENDPOINTING_DELAY_S",
        "max_endpointing_delay=MAX_ENDPOINTING_DELAY_S",
        "allow_interruptions=True,",
        "preemptive_generation=PREEMPTIVE_GENERATION",
    ):
        assert legacy not in src, legacy
    assert '"mode": INTERRUPTION_MODE' in src
    assert '"min_words": MIN_INTERRUPTION_WORDS' in src
    assert '"resume_false_interruption": True' in src
    assert '"mode": ENDPOINTING_MODE' in src


def test_text_rollback_keeps_previous_endpointing_values():
    src = _src()
    assert '"0.3" if _AUDIO_EOT else "0.4"' in src
    assert '"2.5" if _AUDIO_EOT else "1.2"' in src


def test_pyproject_pins_livekit_1_8_across_agents_and_plugins():
    text = _PYPROJECT.read_text(encoding="utf-8")
    pins = re.findall(r'"livekit-(?:agents|plugins-(?:openai|deepgram|elevenlabs|silero|turn-detector))==([0-9.]+)"', text)
    assert len(pins) == 6, pins
    assert set(pins) == {"1.8.0"}, pins


# ── 2. greet-first ───────────────────────────────────────────────────────────


def test_tenant_and_services_are_one_embedded_query():
    src = _src()
    assert '.select("*, services(name, intake_questions, is_active)")' in src
    assert '.eq("services.is_active", True)' in src
    assert 'services_rows = tenant.pop("services", None) or []' in src
    # The former second round-trip is gone.
    assert '.select("name, intake_questions")' not in src


def test_caller_context_loads_after_greeting_and_updates_instructions():
    src = _src()
    i_greet = src.index("greeting_handle = session.say(greeting_text, allow_interruptions=False)")
    i_loader = src.index("async def _load_caller_context():")
    assert i_greet < i_loader, "the caller-context fetch must no longer precede the greeting"
    assert "await agent.update_instructions(full_prompt)" in src
    # Only the per-caller sections are added; the base prompt is built once.
    assert "system_prompt = _render_prompt(customer_context=None, caller_history=None)" in src


def test_unmute_waits_for_greeting_and_context_with_bounds():
    src = _src()
    unmute = src[src.index("async def _unmute_after_greeting():"):src.index("_greeting_unmute_task = create_background_task")]
    assert "greeting_handle.wait_for_playout()" in unmute
    assert "GREETING_UNMUTE_TIMEOUT_S" in unmute
    assert "prompt_ready.wait()" in unmute
    assert "CONTEXT_READY_TIMEOUT_S" in unmute
    assert "set_audio_enabled(True)" in unmute
    # prompt_ready is set in a finally so a failed loader can never hold the mute.
    loader = src[src.index("async def _load_caller_context():"):src.index("_context_task = create_background_task")]
    assert "finally:" in loader and "prompt_ready.set()" in loader


def test_cache_warm_fires_with_final_instructions():
    src = _src()
    loader = src[src.index("async def _load_caller_context():"):src.index("_context_task = create_background_task")]
    assert "_warm_prompt_cache(" in loader
    # and nowhere else (it used to fire right after session.start with the base prompt)
    assert src.count("_warm_prompt_cache(\n") == 1


def test_active_language_is_tracked_from_stt_finals():
    src = _src()
    assert '@session.on("user_input_transcribed")' in src
    assert 'deps["_active_language"] = str(lang).lower()' in src


# ── 3. background audio ──────────────────────────────────────────────────────


def test_background_audio_is_tool_scoped_and_closed():
    from src import agent

    src = _src()
    assert "BackgroundAudioPlayer(ambient_sound=_ambient)" in src
    # The SDK kwarg would play typing on EVERY thinking state (before every
    # sentence); only the log line may mention the name.
    assert not re.search(r"BackgroundAudioPlayer\([^)]*thinking_sound\s*=", src), (
        "typing must be scoped to tool calls via ThinkingSoundController"
    )
    assert "ThinkingSoundController(player, typing_clips)" in src
    assert 'deps["_thinking_ctl"] = ctl' in src
    assert "_player.aclose()" in src
    assert agent.THINKING_SOUND_ENABLED is True
    assert agent.AMBIENT_SOUND_ENABLED is False  # opt-in after a listening test


# ── 4. keyterms ──────────────────────────────────────────────────────────────


def test_stt_keyterms_default_on():
    from src import agent

    assert agent.STT_KEYTERMS_ENABLED is True
    assert '_stt_kwargs["keyterm"] = _keyterms' in _src()
