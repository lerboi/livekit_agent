"""
Phase 64 session-construction invariants (Wave 0 RED).

Asserts that src.agent session assembly uses:
- google.STT(model="chirp_3", languages=..., detect_language=False)   [Pitfall 1 guard]
- google.LLM(model="gemini-3-flash-preview", thinking_config=ThinkingConfig(thinking_level="low", ...))
- silero.VAD.load(min_silence_duration=0.55)  # LiveKit default; D-03b UAT-revised
- GeminiTTS at session-level
- No turn_detector plugin; no google.realtime.RealtimeModel; no 63.1-07 mute scaffolding

These tests are RED in Plan 01 (this file is committed BEFORE the swap). Plan 02 turns
them GREEN by implementing src.agent._build_pipeline_plugins and swapping session assembly.

Contract covered:
- D-01 (big-bang RealtimeModel removal)
- D-03a (remove 63.1-07 input mute scaffolding)
- D-03b (Silero VAD 2.5s silence threshold port)
- D-04 (Silero only, no turn_detector)
- D-05 (google.LLM gemini-3-flash-preview + thinking_config; UAT correction from gemini-3.1-flash which 404s on v1beta)
- D-06 (GeminiTTS session-level)
- D-07 (google.STT chirp_3 with languages= plural + detect_language=False)
- D-09 (preserve all 7 tools)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

AGENT_PY = Path(__file__).parent.parent / "src" / "agent.py"


# ── Text-scan tests (no import of src.agent needed) ────────────────────────────


def _agent_code_only() -> str:
    """Return src/agent.py content with comment-only lines stripped.

    Allows historical commit refs and rationale in `#`-prefixed comments
    without false-positive matches on text-scan assertions.
    """
    content = AGENT_PY.read_text(encoding="utf-8")
    code_lines = [l for l in content.splitlines() if not l.lstrip().startswith("#")]
    return "\n".join(code_lines)


def test_no_realtime_model_reference_in_src():
    """D-01: google.realtime.RealtimeModel must be removed from src/agent.py."""
    code = _agent_code_only()
    assert "google.realtime.RealtimeModel" not in code, (
        "Phase 64 D-01: google.realtime.RealtimeModel must be removed from src/agent.py"
    )


def test_silero_imported():
    """D-04: silero plugin must be imported into src/agent.py."""
    content = AGENT_PY.read_text(encoding="utf-8")
    assert "from livekit.plugins import" in content and "silero" in content, (
        "Phase 64 D-04: silero must be imported into src/agent.py"
    )


def test_no_turn_detector():
    """D-04: Silero-only, no turn_detector plugin."""
    code = _agent_code_only()
    assert "turn_detector" not in code, (
        "Phase 64 D-04: turn_detector plugin must not be used"
    )


def test_no_63_1_07_input_mute_scaffolding():
    """D-03a: 63.1-07 mute-during-greeting workaround must be removed."""
    code = _agent_code_only()
    assert "session.input.set_audio_enabled(False)" not in code, (
        "Phase 64 D-03a: 63.1-07 input mute must be removed"
    )
    assert "_unmute_after_greeting" not in code, (
        "Phase 64 D-03a: _unmute_after_greeting task must be removed"
    )


# ── Plugin-construction tests (import src.agent._build_pipeline_plugins) ──────


def test_build_pipeline_plugins_exists():
    """Plan 02 must export _build_pipeline_plugins helper for this test surface."""
    from src.agent import _build_pipeline_plugins  # noqa: F401


def _patched_plugins(locale: str, voice: str = "Zephyr"):
    """Invoke _build_pipeline_plugins under patches; return recorded kwargs.

    Dispatches recorded constructor kwargs by discriminating `model=` value so
    the test suite is robust against any construction-order choice Plan 02
    makes inside _build_pipeline_plugins. Silero VAD is patched separately
    because it uses the class-method `VAD.load` rather than `__init__`.

    Returns:
        dict with keys "STT", "LLM", "TTS", "VAD.load" — each a list of dicts
        of kwargs captured for every constructor/loader call.
    """
    recorded = {"STT": [], "LLM": [], "TTS": [], "VAD.load": []}

    def _dispatch(self, *args, **kwargs):
        """Route a constructor call to its bucket by `model=` kwarg."""
        model = kwargs.get("model")
        cls_name = self.__class__.__name__
        if model == "chirp_3" or cls_name == "STT":
            recorded["STT"].append(dict(kwargs))
        elif model == "gemini-3-flash-preview" or cls_name == "LLM":
            recorded["LLM"].append(dict(kwargs))
        elif model == "gemini-2.5-flash-preview-tts" or cls_name == "TTS":
            recorded["TTS"].append(dict(kwargs))
        else:
            raise AssertionError(
                f"_patched_plugins saw an unrecognized constructor: "
                f"class={cls_name} model={model!r} kwargs={list(kwargs)}"
            )

    def _fake_vad_load(**kwargs):
        recorded["VAD.load"].append(dict(kwargs))
        return MagicMock()

    with patch("livekit.plugins.google.STT.__init__", _dispatch), \
         patch("livekit.plugins.google.LLM.__init__", _dispatch), \
         patch("livekit.plugins.google.beta.gemini_tts.TTS.__init__", _dispatch), \
         patch("livekit.plugins.silero.VAD.load", _fake_vad_load):
        from src.agent import _build_pipeline_plugins
        _build_pipeline_plugins(locale=locale, voice_name=voice)
    return recorded


def test_stt_languages_kwarg_plural_spanish():
    """Pitfall 1 silent-failure guard: ES locale must use `languages=` (plural)."""
    rec = _patched_plugins(locale="es")
    assert len(rec["STT"]) == 1, "expected exactly one google.STT constructor call"
    stt_kwargs = rec["STT"][0]
    assert "languages" in stt_kwargs, (
        "google.STT must be called with `languages=` (plural)"
    )
    assert "language" not in stt_kwargs, (
        "google.STT must NOT be called with singular `language=` (Pitfall 1 silent failure)"
    )
    assert stt_kwargs["languages"] == "es-US"


def test_stt_languages_kwarg_english():
    rec = _patched_plugins(locale="en")
    assert len(rec["STT"]) == 1
    stt_kwargs = rec["STT"][0]
    assert stt_kwargs["languages"] == "en-US"


def test_stt_detect_language_disabled():
    """Pitfall 2 guard: detect_language=False required when languages= is pinned."""
    rec = _patched_plugins(locale="en")
    stt_kwargs = rec["STT"][0]
    assert stt_kwargs["detect_language"] is False


def test_stt_model_is_chirp_3():
    rec = _patched_plugins(locale="en")
    stt_kwargs = rec["STT"][0]
    assert stt_kwargs["model"] == "chirp_3"


def test_llm_model_is_gemini_3_flash_preview():
    """Phase 64 UAT correction: AI Studio v1beta returns 404 for 'gemini-3.1-flash'
    (the D-05 originally-locked name). The actual Gemini 3 Flash identifier is
    'gemini-3-flash-preview' per https://ai.google.dev/gemini-api/docs/gemini-3 .
    The Pro variant uses '3.1' (gemini-3.1-pro-preview); the Flash variant does not."""
    rec = _patched_plugins(locale="en")
    assert len(rec["LLM"]) == 1, "expected exactly one google.LLM constructor call"
    llm_kwargs = rec["LLM"][0]
    assert llm_kwargs["model"] == "gemini-3-flash-preview"


def test_llm_thinking_config_low():
    rec = _patched_plugins(locale="en")
    llm_kwargs = rec["LLM"][0]
    tc = llm_kwargs["thinking_config"]
    # genai_types.ThinkingConfig normalizes `thinking_level="low"` into the
    # ThinkingLevel.LOW enum whose `.value` is the uppercase string "LOW".
    # Accept either representation — the semantic contract is "low tier
    # thinking", not the exact case of the stored enum value.
    level_str = getattr(tc.thinking_level, "value", tc.thinking_level)
    assert str(level_str).lower() == "low"
    assert tc.include_thoughts is False


def test_vad_min_silence_duration_default():
    """D-03b UAT revision: Phase 63.1-11's 2500ms was a Realtime server-VAD parameter,
    NOT a Silero parameter. Porting it to Silero produced ~11s end-of-turn latency
    that caused callers to hang up before responses finished buffering. Reverted to
    LiveKit's documented Silero default per
    https://docs.livekit.io/agents/build/turns/vad/ .
    """
    rec = _patched_plugins(locale="en")
    assert len(rec["VAD.load"]) == 1, "expected exactly one silero.VAD.load call"
    vad_kwargs = rec["VAD.load"][0]
    assert vad_kwargs["min_silence_duration"] == 0.55


def test_tts_is_gemini_tts_session_level():
    """D-06: GeminiTTS promoted from greeting-only to session-level tts=."""
    rec = _patched_plugins(locale="en", voice="Zephyr")
    assert len(rec["TTS"]) == 1, "expected exactly one GeminiTTS constructor call"
    tts_kwargs = rec["TTS"][0]
    assert tts_kwargs["voice_name"] == "Zephyr"
    assert tts_kwargs["model"] == "gemini-2.5-flash-preview-tts"
    assert tts_kwargs.get("instructions"), (
        "GeminiTTS must be constructed with non-empty instructions"
    )


# ── Tool-registration guard (D-09 regression) ─────────────────────────────────


def test_seven_tools_registered():
    """D-09: All 7 tools must still be registered on the pipeline session.

    `create_tools(deps: dict)` returns:
      - 5 always-available tools (transfer_call, capture_lead,
        check_caller_history, check_customer_account, end_call)
      - + check_availability + book_appointment when onboarding_complete=True

    Total post-onboarding: 7 tools.
    """
    from src.tools import create_tools

    deps = {
        "call_id": "test-call-id",
        "tenant_id": "00000000-0000-0000-0000-000000000000",
        "from_number": "+6587528516",
        "call_end_reason": ["caller_hangup"],
        "_tool_call_log": [],
        "_diag_record": [{}],
        "onboarding_complete": True,
    }
    tools = create_tools(deps)
    assert len(tools) == 7, f"Expected 7 tools post-onboarding, got {len(tools)}"

    # Tool callables carry their registered name via @function_tool; fall back
    # to stringified repr if attribute missing.
    names_blob = "|".join(
        getattr(t, "__name__", "") or getattr(t, "name", "") or repr(t)
        for t in tools
    )
    expected = {
        "check_availability",
        "book_appointment",
        "capture_lead",
        "check_caller_history",
        "check_customer_account",
        "transfer_call",
        "end_call",
    }
    for tool_name in expected:
        assert tool_name in names_blob, (
            f"Tool {tool_name} missing from pipeline tool set (D-09 regression)"
        )
