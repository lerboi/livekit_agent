"""Phase 60.4 Stream B — _build_language_section anti-hallucination directive (D-B-03).

EN and ES branches must both carry a directive telling Gemini to treat
unintelligible audio as the caller's default locale — not to invent
foreign-language tokens during silence. Parity with 60.3 Plan 12 closure
must be preserved (existing "Default to English" / "Por defecto en español"
defaults stay intact).
"""
from __future__ import annotations

import re

import pytest

from src.prompt import _build_language_section


def test_en_language_section_has_anti_hallucination_directive():
    out = _build_language_section(None, locale="en").lower()
    # Must mention the unintelligibility/garbled concept AND pin the response
    # language back to English.
    assert re.search(r"garbled|unintelligible|muffled", out), (
        "EN branch missing unintelligibility directive"
    )
    assert "english" in out
    assert "anti-hallucination" in out or "never invent" in out, (
        "EN branch should carry an explicit anti-hallucination framing"
    )


def test_es_language_section_has_anti_hallucination_directive():
    out = _build_language_section(None, locale="es").lower()
    # Spanish mirror — mention the unintelligibility concept in ES and pin
    # the response language back to Spanish.
    assert re.search(r"distorsionado|ininteligible|apagado", out), (
        "ES branch missing unintelligibility directive"
    )
    assert "español" in out
    assert "anti-alucinación" in out or "nunca invente" in out, (
        "ES branch should carry an explicit anti-hallucination framing"
    )


def test_language_section_en_es_parity_character_delta_within_30pct():
    en = _build_language_section(None, locale="en")
    es = _build_language_section(None, locale="es")
    longer = max(len(en), len(es))
    shorter = min(len(en), len(es))
    delta = (longer - shorter) / longer
    assert delta <= 0.30, (
        f"EN/ES length drift > 30% (en={len(en)}, es={len(es)}, delta={delta:.2%})"
    )


def test_language_section_preserves_60_3_plan_12_defaults():
    en = _build_language_section(None, locale="en")
    es = _build_language_section(None, locale="es")
    assert "Default to English" in en, "60.3 Plan 12 EN default lost"
    assert "Por defecto en español" in es, "60.3 Plan 12 ES default lost"
