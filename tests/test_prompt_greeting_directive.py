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


# ──────────────────────────────────────────────────────────────────────────
# Phase 64 D-03c — re-frame greeting directive for pipeline architecture.
#
# On pipeline path, session.say(greeting_text) delivers the branded greeting
# as assistant-turn-1 in the LLM's chat history. The guardrail's job is
# unchanged (don't re-greet) but its framing shifts from a workaround
# ("GREETING ALREADY PLAYED — DO NOT REPEAT") to an architectural fact
# ("Greeting already delivered via system; respond directly").
#
# Tests below are RED in Plan 01 — they fail against the current 63.1-06
# prose. Plan 03 turns them GREEN by re-framing _build_greeting_section.
# Pre-existing tests above must continue to pass (the re-frame preserves
# the no-re-greet contract; only the framing changes).
# ──────────────────────────────────────────────────────────────────────────


def test_en_greeting_re_framed_as_delivered_via_system():
    """D-03c: EN greeting re-framed as 'already delivered via system'."""
    section = _greeting_en()
    lower = section.lower()
    accepted = [
        "greeting already delivered",
        "greeting was already delivered",
        "greeting has already been delivered",
    ]
    assert any(phrase in lower for phrase in accepted), (
        f"Phase 64 D-03c: EN greeting must re-frame as 'already delivered via system' "
        f"(none of {accepted} found).\nSection:\n{section}"
    )
    # Outcome-shaped — directive must steer the model to respond to caller input
    assert "respond" in lower, (
        "Phase 64 D-03c: EN re-frame must direct model to respond to caller input"
    )


def test_en_greeting_does_not_use_legacy_workaround_header():
    """D-03c RED: forces removal of the 63.1-06 workaround-shaped header."""
    section = _greeting_en()
    assert "GREETING ALREADY PLAYED — DO NOT REPEAT" not in section, (
        "Phase 64 D-03c: legacy 63.1-06 workaround header must be re-framed"
    )


def test_es_greeting_re_framed_as_delivered_via_system():
    """D-03c: ES greeting re-framed with USTED register + outcome-shaped verb."""
    section = _greeting_es()
    lower = section.lower()
    accepted = [
        "ya ha sido entregado",
        "ya fue entregado",
        "el sistema ya entregó",
        "el sistema ya ha entregado",
        "saludo ya entregado",
        "saludo ha sido entregado",
    ]
    assert any(phrase in lower for phrase in accepted), (
        f"Phase 64 D-03c: ES greeting must carry a 'system-delivered' re-frame "
        f"(none of {accepted} found).\nSection:\n{section}"
    )
    # USTED imperative — "responda" (not "responde" tú-form) directs model to reply
    assert "responda" in lower, (
        "Phase 64 D-03c: ES re-frame must use USTED imperative 'responda'"
    )


def test_es_greeting_does_not_use_legacy_workaround_header():
    """D-03c RED: forces removal of the 63.1-06 ES workaround header."""
    section = _greeting_es()
    assert "SALUDO YA REALIZADO — NO SE REPITA" not in section, (
        "Phase 64 D-03c: legacy 63.1-06 ES workaround header must be re-framed"
    )


def test_both_locales_retain_do_not_re_greet_guardrail():
    """D-03c: re-frame preserves no-re-greet contract in both locales.

    The FRAMING changes (workaround → architectural fact), but the
    behavioral contract (model must not repeat the greeting) is preserved.
    """
    en = _greeting_en().lower()
    es = _greeting_es().lower()
    # EN: any of several acceptable no-re-greet phrasings
    assert ("do not repeat" in en) or ("not repeat the greeting" in en) or ("do not re-greet" in en), (
        "Phase 64 D-03c: EN re-frame must preserve a 'do not repeat the greeting' guardrail"
    )
    # ES: USTED-register negative imperative
    assert ("no repita" in es) or ("no repetir" in es) or ("no se repita" in es), (
        "Phase 64 D-03c: ES re-frame must preserve a 'no repita' guardrail"
    )


def test_business_name_still_interpolated_both_locales_after_reframe():
    """D-03c regression guard: business_name substring preserved after re-frame."""
    en = _greeting_en(business_name="AcmeCorp")
    es = _greeting_es(business_name="AcmeCorp")
    assert "AcmeCorp" in en, (
        "Phase 64 D-03c: EN re-frame must still interpolate business_name "
        "(Phase 63.1-06 contract preserved)"
    )
    assert "AcmeCorp" in es, (
        "Phase 64 D-03c: ES re-frame must still interpolate business_name "
        "(Phase 63.1-06 contract preserved)"
    )
