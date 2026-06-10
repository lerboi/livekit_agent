"""Phase 60.3 Plan 08 — _build_corrections_section locale parity.

Stream B patch addresses 60.3-PROMPT-AUDIT.md §_build_corrections_section
(dimensions D1, D2, D4, D7). Primary goal: close the D7 (locale parity) gap
by adding a Spanish branch — caller-correction handling is the highest-risk
anti-hallucination surface after _build_outcome_words_section. Spanish
callers whose name or address was misheard need the same aggressive-discard
protocol.

Invariants asserted here:
1. EN output contains the "HANDLING CORRECTIONS — CRITICAL RULE:" header.
2. ES output contains the "MANEJO DE CORRECCIONES — REGLA CRÍTICA:" header.
3. Both locales preserve the five numbered rules ("1." … "5.").
4. Both locales carry a concrete address example:
   EN "123 Main" + "456 Oak"; ES "Calle Principal 123" + "Avenida Roble 456".
5. Both locales carry the discard rule
   (EN "completely discard"; ES "descarta completamente").
6. Both locales assert the caller's correction is always correct
   (EN "always correct"; ES "siempre correcto" OR "siempre es correcto").
7. EN and ES strings are distinct (parity guard against copy-paste error).
"""
from __future__ import annotations

from src.prompt import _build_corrections_section


def test_en_critical_rule_heading():
    section = _build_corrections_section("en")
    assert isinstance(section, str)
    assert "HANDLING CORRECTIONS:" in section


def test_es_critical_rule_heading():
    # 2026-06-11 single-prompt collapse: locale="es" returns the same English
    # body — invariant (corrections rule present for es-locale calls) maps to
    # the EN header.
    section = _build_corrections_section("es")
    assert isinstance(section, str)
    assert "HANDLING CORRECTIONS:" in section


def test_en_five_numbered_rules():
    section = _build_corrections_section("en")
    for n in ("1.", "2.", "3.", "4.", "5."):
        assert n in section, f"EN missing rule {n}"


def test_es_five_numbered_rules():
    section = _build_corrections_section("es")
    for n in ("1.", "2.", "3.", "4.", "5."):
        assert n in section, f"ES missing rule {n}"


def test_en_address_example():
    section = _build_corrections_section("en")
    assert "123 Main" in section
    assert "456 Oak" in section


def test_es_address_example():
    # 2026-06-11 collapse: the concrete-example invariant maps to the EN
    # example (the ES example was a pure localization of the same teaching).
    section = _build_corrections_section("es")
    assert "123 Main" in section
    assert "456 Oak" in section


def test_both_locales_discard_rule():
    # 2026-06-11 collapse: discard rule unchanged; both locales carry EN text.
    for locale in ("en", "es"):
        assert "completely discard" in _build_corrections_section(locale).lower()


def test_both_locales_always_correct():
    # 2026-06-11 collapse: caller-correction-always-wins unchanged; EN text
    # in both locales.
    for locale in ("en", "es"):
        assert "always correct" in _build_corrections_section(locale).lower()


def test_en_es_identical():
    # 2026-06-11 collapse: the old distinctness guard inverts — this section
    # must NOT fork on locale anymore.
    en = _build_corrections_section("en")
    es = _build_corrections_section("es")
    assert en == es
