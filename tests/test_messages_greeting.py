"""P1.9 (2026-09-04): shorter deterministic greeting + ES diacritics.

The greeting is spoken by session.say() with caller input MUTED, so every
word is dead air for the caller. 22 words → ~12, disclosure kept as one
clause (US two-party states; SG PDPA purpose notice). No test pinned the
previous wording; these pin the new contract so it does not creep back up.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_MSG = Path(__file__).resolve().parent.parent / "src" / "messages"
_EN = json.loads((_MSG / "en.json").read_text(encoding="utf-8"))["agent"]
_ES = json.loads((_MSG / "es.json").read_text(encoding="utf-8"))["agent"]


@pytest.mark.parametrize("key", ["greeting_onboarding", "greeting_default"])
def test_en_greeting_is_short_and_keeps_disclosure(key):
    text = _EN[key].format(business_name="Ace Plumbing")
    assert len(text.split()) <= 14, text
    assert "recorded" in text.lower()
    assert "how can i help" in text.lower()


def test_en_greeting_onboarding_carries_business_name_placeholder():
    assert "{business_name}" in _EN["greeting_onboarding"]
    assert "{business_name}" not in _EN["greeting_default"]


@pytest.mark.parametrize("key", ["greeting_onboarding", "greeting_default"])
def test_es_greeting_is_short_with_diacritics(key):
    text = _ES[key].format(business_name="Ace Plumbing")
    assert len(text.split()) <= 16, text
    assert "graba" in text
    assert "¿en qué puedo ayudarle?" in text


def test_recording_disclosure_key_retained():
    """tests/test_prompt_tail_sections.py reads this key to assert the
    disclosure is NOT inlined in the prompt — the key must stay."""
    assert _EN["recording_disclosure"]
    assert _ES["recording_disclosure"]


def test_recovery_error_no_longer_promises_to_take_details():
    """The recovery path speaks this line and hangs up — it must not promise
    'let me take your details'."""
    for text in (_EN["recovery_error"], _ES["recovery_error"]):
        assert "take your details" not in text.lower()
        assert "tomar sus datos" not in text.lower()
    assert "call you right back" in _EN["recovery_error"]
    assert "devolverá la llamada" in _ES["recovery_error"]


def test_es_agent_strings_use_inverted_punctuation_and_accents():
    questions = [v for v in _ES.values() if v.rstrip().endswith("?")]
    assert questions
    for q in questions:
        assert "¿" in q, q
    accented = "".join(_ES.values())
    assert any(ch in accented for ch in "áéíóúñ")


def test_en_es_agent_key_sets_match():
    assert set(_EN) == set(_ES)
    for k in ("greeting_onboarding",):
        assert "{business_name}" in _ES[k]
