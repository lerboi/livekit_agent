"""Phase 65 hotfix — `_resolve_voice` guards against stale/unsupported ai_voice.

Production incident (2026-06-05, tenant "Make It AI" 24141cd0): after the
gpt-realtime-2 cutover, `tenants.ai_voice` still held the Gemini-era voice
"Zephyr" (migration 067, which NULLs ai_voice + swaps the CHECK, had not been
applied to the prod DB). gpt-realtime-2 accepts only the OpenAI realtime voice
set, so `RealtimeModel(voice="Zephyr")` errored the ENTIRE session on every
call. The agent must never pass an unvalidated voice to OpenAI — it falls back
to the tone-based default instead.
"""
from src.agent import OPENAI_VOICES, VOICE_MAP, _resolve_voice


def test_valid_openai_voice_is_used_as_is():
    assert _resolve_voice("cedar", "professional") == "cedar"
    assert _resolve_voice("marin", "friendly") == "marin"
    assert _resolve_voice("verse", "local_expert") == "verse"


def test_stale_gemini_voice_falls_back_to_tone_default():
    # The exact incident value.
    assert _resolve_voice("Zephyr", "professional") == "marin"
    # The other curated Gemini voices from migration 044.
    for gemini_voice in ("Aoede", "Erinome", "Sulafat", "Achird", "Charon"):
        assert _resolve_voice(gemini_voice, "friendly") == "cedar"


def test_none_falls_back_to_tone_default():
    assert _resolve_voice(None, "professional") == "marin"
    assert _resolve_voice(None, "friendly") == "cedar"
    assert _resolve_voice(None, "local_expert") == "alloy"


def test_unknown_tone_falls_back_to_marin():
    assert _resolve_voice(None, "totally_unknown") == "marin"
    assert _resolve_voice("Zephyr", "totally_unknown") == "marin"


def test_empty_string_falls_back_to_tone_default():
    assert _resolve_voice("", "professional") == "marin"


def test_voice_map_values_are_all_valid_openai_voices():
    # The fallback must never itself be an unsupported voice.
    for voice in VOICE_MAP.values():
        assert voice in OPENAI_VOICES
