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
    # 2026-06-11 single-prompt collapse: the es-locale section is the same
    # unified English text — the invariant (anti-hallucination directive
    # covers es-locale calls) maps to the EN pins, plus the Spanish-default
    # line that is now locale's ONLY effect.
    out = _build_language_section(None, locale="es")
    lowered = out.lower()
    assert re.search(r"garbled|unintelligible|muffled", lowered), (
        "es-locale output missing unintelligibility directive"
    )
    assert "anti-hallucination" in lowered or "never invent" in lowered, (
        "es-locale output should carry an explicit anti-hallucination framing"
    )
    assert "This business operates in Spanish" in out, (
        "es-locale output must carry the default-to-Spanish line"
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
    # 2026-06-11 collapse: the ES default pin moves from "Por defecto en
    # español" to the new English-stated Spanish-default line — same
    # invariant (locale still flips the tenant's default language).
    en = _build_language_section(None, locale="en")
    es = _build_language_section(None, locale="es")
    assert "Default to English" in en, "60.3 Plan 12 EN default lost"
    assert (
        "This business operates in Spanish — open in Spanish and default to "
        "Spanish on every call." in es
    ), "default-to-Spanish line lost"
