"""Phase 65 — tests for the OPENING directive in `_build_greeting_section`.

gpt-realtime-2 supports agent-first turns, so the opening greeting is now
delivered NATIVELY: src/agent.py calls `session.generate_reply(...)` right
after `session.start()`. The system prompt's greeting section therefore tells
the model HOW to open the call — a warm branded greeting + the recording
disclosure + an offer to help — and to greet only ONCE. This replaces the
Gemini-era "GREETING ALREADY PLAYED — DO NOT REPEAT" framing, which existed
only because the 3.1 separate-TTS hack spoke the greeting before the model's
first turn.

Assertions are scoped to `_build_greeting_section` output (not the full
assembled prompt) so unrelated sections cannot leak a false-GREEN signal.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.prompt import _build_greeting_section

_messages_dir = Path(__file__).parent.parent / "src" / "messages"


def _load(name: str) -> dict:
    with open(_messages_dir / f"{name}.json", "r", encoding="utf-8") as f:
        return json.load(f)


_en = _load("en")
_es = _load("es")


def _mk_t(msgs: dict):
    def t(key: str) -> str:
        val: object = msgs
        for part in key.split("."):
            if isinstance(val, dict):
                val = val.get(part)
            else:
                return key
        return val if isinstance(val, str) else key

    return t


def _greeting_en(business_name: str = "AcmeCorp", onboarding_complete: bool = True) -> str:
    return _build_greeting_section("en", business_name, onboarding_complete, _mk_t(_en))


def _greeting_es(business_name: str = "AcmeCorp", onboarding_complete: bool = True) -> str:
    return _build_greeting_section("es", business_name, onboarding_complete, _mk_t(_es))


def test_greeting_en_instructs_model_to_open_and_greet_once():
    out = _greeting_en().lower()
    assert "opening:" in out
    assert "you open the call" in out
    assert "greet once" in out
    assert "do not greet again" in out
    assert "respond directly" in out


def test_greeting_es_instructs_model_to_open_and_greet_once():
    out = _greeting_es().lower()
    assert "apertura:" in out
    assert "salude una sola vez" in out
    assert "no vuelva a saludar" in out
    assert "directamente" in out


def test_greeting_includes_recording_disclosure_en():
    out = _greeting_en().lower()
    assert "this call may be recorded for quality purposes" in out


def test_greeting_includes_recording_disclosure_es():
    out = _greeting_es().lower()
    assert "esta llamada puede ser grabada por motivos de calidad" in out


def test_greeting_references_business_name_en():
    out = _greeting_en(business_name="AcmeCorp").lower()
    assert "acmecorp" in out


def test_greeting_references_business_name_es():
    out = _greeting_es(business_name="AcmeCorp").lower()
    assert "acmecorp" in out


def test_greeting_not_onboarding_complete_omits_business_name_en():
    # When onboarding isn't complete, the example opening drops the brand line
    # (booking is not yet available) but still carries the disclosure + offer.
    out = _greeting_en(business_name="AcmeCorp", onboarding_complete=False).lower()
    assert "acmecorp" not in out
    assert "this call may be recorded for quality purposes" in out


def test_greeting_no_stale_gemini_tts_framing():
    # The Gemini-era "already played / do not repeat the greeting" framing must
    # be gone in both locales.
    en = _greeting_en().lower()
    es = _greeting_es().lower()
    assert "already" not in en
    assert "do not repeat the greeting" not in en
    assert "ya realizado" not in es
