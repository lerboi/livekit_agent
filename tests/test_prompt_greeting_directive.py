"""Phase 63.1-06 — tests for the do-not-re-greet directive in
`_build_greeting_section`.

The opening greeting is now played by a separate TTS pipeline (see
`src/agent.py` `session.say(...)` after `session.start()`) because
Gemini 3.1 Flash Live capability-gates all three standard "speak first"
APIs closed. The system prompt's job is therefore INVERTED from the
pre-63.1-06 state: instead of asking the model to greet, it must
instruct the model NOT to greet — the greeting has already played by
the time Gemini's first generation runs.

Tests assert the directive is present and locale-parity is preserved.
Assertions are scoped to `_build_greeting_section` output (not the
full assembled prompt) so unrelated sections (e.g. Phase 60.3 Plan 03
`_build_call_duration_section`'s WRONG-example string) cannot leak a
false-GREEN signal.
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


def test_greeting_directive_en_instructs_no_repeat():
    out = _greeting_en().lower()
    assert "already" in out and "do not repeat" in out
    assert "respond directly" in out


def test_greeting_directive_es_instructs_no_repeat():
    out = _greeting_es().lower()
    assert "ya realizado" in out or "ya" in out
    assert "no se repita" in out or "no repita" in out
    assert "directamente" in out


def test_greeting_directive_references_business_name_en():
    out = _greeting_en(business_name="AcmeCorp").lower()
    assert "acmecorp" in out


def test_greeting_directive_references_business_name_es():
    out = _greeting_es(business_name="AcmeCorp").lower()
    assert "acmecorp" in out


def test_greeting_directive_forbids_re_greeting_en():
    out = _greeting_en().lower()
    # Model must be told not to say hello/announce business at start
    assert "do not say hello" in out or "do not repeat the greeting" in out
    # Must NOT ask the model to proactively say "thank you for calling"
    # as an instruction (the TTS already said it; we reference it quoted
    # inside the directive but the directive itself forbids repetition).
    assert "do not" in out  # structural guard: directive must be in forbidding form


def test_greeting_directive_forbids_re_greeting_es():
    out = _greeting_es().lower()
    assert "no diga hola" in out or "no repita el saludo" in out
    assert "no " in out  # structural guard: directive uses negation
