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
    assert "HANDLING CORRECTIONS — CRITICAL RULE:" in section


def test_es_critical_rule_heading():
    section = _build_corrections_section("es")
    assert isinstance(section, str)
    assert "MANEJO DE CORRECCIONES — REGLA CRÍTICA:" in section


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
    section = _build_corrections_section("es")
    assert "Calle Principal 123" in section
    assert "Avenida Roble 456" in section


def test_both_locales_discard_rule():
    en = _build_corrections_section("en").lower()
    es = _build_corrections_section("es").lower()
    assert "completely discard" in en
    assert "descarta completamente" in es


def test_both_locales_always_correct():
    en = _build_corrections_section("en").lower()
    es = _build_corrections_section("es").lower()
    assert "always correct" in en
    # Accept either phrasing of the Spanish equivalent.
    assert ("siempre correcto" in es) or ("siempre es correcto" in es)


def test_en_es_distinct():
    en = _build_corrections_section("en")
    es = _build_corrections_section("es")
    assert en != es
