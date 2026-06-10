"""Phase 66 — tests for the reframed OPENING directive in `_build_greeting_section`.

The cascaded pipeline delivers the opening greeting deterministically via
`session.say(<template>)` BEFORE the LLM's first turn, so the prompt's greeting
section no longer tells the model to "open the call". Instead it tells the model
the greeting was ALREADY delivered by the system and to respond to the caller
without re-greeting. (Phase 65 used native generate_reply + a "you open the call"
directive; this reframes it — leaving "you open the call" in place would make the
model re-greet on its first real turn.)

Assertions are scoped to `_build_greeting_section` output (not the full assembled
prompt) so unrelated sections cannot leak a false-GREEN signal.
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


def test_greeting_en_reframed_to_already_delivered():
    out = _greeting_en().lower()
    assert "opening:" in out
    assert "already spoken" in out
    assert "do not greet again" in out
    assert "respond directly" in out


def test_greeting_es_reframed_to_already_delivered():
    # 2026-06-11 single-prompt collapse: locale="es" returns the same EN
    # section (the spoken session.say greeting itself stays per-locale via
    # messages/es.json) — the already-delivered framing pins map to EN.
    out = _greeting_es().lower()
    assert "opening:" in out
    assert "already spoken" in out
    assert "do not greet again" in out
    assert "respond directly" in out


def test_greeting_keeps_echo_awareness_note():
    # 2026-06-11 collapse: echo-awareness invariant unchanged; EN in both.
    assert "echo awareness:" in _greeting_en().lower()
    assert "echo awareness:" in _greeting_es().lower()


def test_greeting_drops_you_open_the_call_framing():
    # The deterministic session.say greeting means the model must NOT be told to
    # open the call (that would make it re-greet on its first real turn).
    en = _greeting_en().lower()
    es = _greeting_es().lower()
    assert "you open the call" not in en
    assert "greet once" not in en
    assert "usted abre la llamada" not in es
    assert "salude una sola vez" not in es


def test_greeting_section_does_not_inline_the_disclosure_text():
    # The disclosure is spoken by session.say from the message template, not by
    # the model — the section only refers to it, never reproduces it verbatim.
    assert "This call may be recorded for quality purposes" not in _greeting_en()
    assert "Esta llamada puede ser grabada por motivos de calidad" not in _greeting_es()


def test_greeting_section_is_locale_independent():
    # 2026-06-11 collapse: the old locale-specific guard inverts — the
    # greeting SECTION must not fork on locale (the spoken greeting template
    # in messages/{en,es}.json remains per-locale and untouched).
    assert _greeting_en() == _greeting_es()
