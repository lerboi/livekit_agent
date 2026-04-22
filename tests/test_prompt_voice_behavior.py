"""Phase 60.3 Plan 07 — _build_voice_behavior_section locale parity.

Stream B patch addresses 60.3-PROMPT-AUDIT.md §_build_voice_behavior_section
(dimensions D2, D4, D5, D7). Primary goal: close the D7 (locale parity) gap
by adding a Spanish branch. EN body is preserved from the post-60.2 state
(minimal change — the audit flagged D5 VAD-redundant acknowledgment-pacing
copy, but the acknowledgment semantics are load-bearing for realtime
back-and-forth coaching; D5 compression deferred per audit Decision note).

Invariants asserted here:
1. EN output contains the "VOICE & CONVERSATION STYLE:" header.
2. ES output contains the "ESTILO DE VOZ Y CONVERSACIÓN:" header.
3. Both locales instruct the model to match the caller's energy
   (EN: "match the caller's energy"; ES: "coincide con la energía").
4. Both locales advise slow readback of addresses/times
   (EN: "slow down" + addresses/dates; ES: "más despacio" + direcciones/fechas).
5. EN and ES strings are distinct (parity guard against copy-paste error).
6. Both locales are non-trivial (length > 200 chars).
"""
from __future__ import annotations

from src.prompt import _build_voice_behavior_section


def test_en_contains_voice_style_heading():
    section = _build_voice_behavior_section("en")
    assert isinstance(section, str)
    assert "VOICE & CONVERSATION STYLE:" in section


def test_es_contains_voice_style_heading():
    section = _build_voice_behavior_section("es")
    assert isinstance(section, str)
    assert "ESTILO DE VOZ Y CONVERSACIÓN:" in section


def test_both_locales_instruct_energy_matching():
    en = _build_voice_behavior_section("en").lower()
    es = _build_voice_behavior_section("es").lower()
    # EN: "match the caller's energy"
    assert "match the caller's energy" in en
    # ES: "coincide con la energía"
    assert "coincide con la energía" in es


def test_both_locales_advise_slow_readback():
    en = _build_voice_behavior_section("en").lower()
    es = _build_voice_behavior_section("es").lower()
    # EN: "slow down" + references to addresses and dates
    assert "slow down" in en
    assert "addresses" in en
    assert "dates" in en
    # ES: "más despacio" + references to direcciones and fechas
    assert "más despacio" in es
    assert "direcciones" in es
    assert "fechas" in es


def test_en_and_es_are_distinct():
    en = _build_voice_behavior_section("en")
    es = _build_voice_behavior_section("es")
    assert en != es


def test_both_locales_nonempty_nontrivial():
    en = _build_voice_behavior_section("en")
    es = _build_voice_behavior_section("es")
    assert len(en) > 200
    assert len(es) > 200
