"""Phase 66 — `_resolve_voice` maps a stored ai_voice LABEL (or tone_preset) to
an ElevenLabs voice_id, with safe fallbacks.

tenants.ai_voice stores a stable LABEL (professional / friendly / local_expert)
or NULL after migration 068. A stale value from a prior era — OpenAI voices
("marin", "cedar") or Gemini voices ("Zephyr") — is NOT a known label, so it
falls back to the tone_preset's voice; an unknown tone falls back to
professional. The agent must never pass an unknown value straight through to
ElevenLabs. Voice IDs themselves are config (placeholders until D4) so these
tests assert resolution *logic* against the map, never specific id strings.
"""
from src.agent import ELEVENLABS_VOICE_MAP, ELEVENLABS_VOICE_LABELS, _resolve_voice


def test_known_label_resolves_to_its_voice_id():
    for label in ("professional", "friendly", "local_expert"):
        assert _resolve_voice(label, "professional") == ELEVENLABS_VOICE_MAP[label]


def test_stale_openai_or_gemini_voice_falls_back_to_tone_default():
    # Stored values from the prior OpenAI (marin/cedar/verse/alloy) and Gemini
    # (Zephyr/Aoede) eras are not labels -> resolve by tone_preset instead.
    assert _resolve_voice("marin", "professional") == ELEVENLABS_VOICE_MAP["professional"]
    assert _resolve_voice("cedar", "friendly") == ELEVENLABS_VOICE_MAP["friendly"]
    for stale in ("Zephyr", "Aoede", "verse", "alloy"):
        assert _resolve_voice(stale, "local_expert") == ELEVENLABS_VOICE_MAP["local_expert"]


def test_none_falls_back_to_tone_default():
    assert _resolve_voice(None, "professional") == ELEVENLABS_VOICE_MAP["professional"]
    assert _resolve_voice(None, "friendly") == ELEVENLABS_VOICE_MAP["friendly"]
    assert _resolve_voice(None, "local_expert") == ELEVENLABS_VOICE_MAP["local_expert"]


def test_unknown_tone_falls_back_to_professional():
    assert _resolve_voice(None, "totally_unknown") == ELEVENLABS_VOICE_MAP["professional"]
    assert _resolve_voice("Zephyr", "totally_unknown") == ELEVENLABS_VOICE_MAP["professional"]


def test_empty_string_falls_back_to_tone_default():
    assert _resolve_voice("", "professional") == ELEVENLABS_VOICE_MAP["professional"]


def test_labels_are_exactly_the_three_tone_presets():
    assert ELEVENLABS_VOICE_LABELS == frozenset({"professional", "friendly", "local_expert"})


def test_every_label_maps_to_a_nonempty_voice_id_string():
    # Passes for placeholders and for real ids — the contract is "always a
    # non-empty string"; the live UAT call verifies the id actually voices.
    for label in ELEVENLABS_VOICE_LABELS:
        vid = ELEVENLABS_VOICE_MAP[label]
        assert isinstance(vid, str) and vid != ""
