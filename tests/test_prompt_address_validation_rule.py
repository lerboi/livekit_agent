"""Phase 61 Plan 04 — _build_address_validation_section locale parity (D-E3).

Stream B patch addresses 61-RESEARCH.md §D-E3 — a new CRITICAL RULE for the
"validated" truth-class. Co-located with the existing anti-hallucination
spine (corrections/outcome_words) in the top-attention zone. Spanish mirror
in the same pass per Phase 60.3 D-B-03 locale-parity pattern.

The rule prohibits 6 verbatim phrases ("validated", "verified",
"confirmed against Google", "found your address", "looked up your
address", "matches our records") UNLESS the immediately preceding tool
return contained `verdict=validated` or `verdict=validated_with_corrections`.

A Spanish caller fabricating "validado" without the verdict to back it up is
identically catastrophic — hence the locale-parity mandate. Tool-return
verdict tokens (`verdict=validated`, `verdict=validated_with_corrections`)
are CODE IDENTIFIERS, not prose — they MUST NOT be translated.

Invariants asserted here:
1. EN output contains the "ADDRESS VALIDATION — CRITICAL RULE:" header.
2. ES output contains the "VALIDACIÓN DE DIRECCIÓN — REGLA CRÍTICA:" header.
3. EN enumerates the 6 prohibited phrases verbatim (>=5 of 6).
4. ES enumerates the 6 Spanish prohibited phrases (>=4 of 6).
5. Both locales reference both verdict tokens
   (`verdict=validated` AND `verdict=validated_with_corrections`).
6. Both locales declare silence/neutral readback acceptable.
7. The new section appears in the top-attention zone of the assembled prompt
   — specifically BEFORE `_build_tool_narration_section`'s output.
8. EN and ES strings are distinct (parity guard against copy-paste error).
"""
from __future__ import annotations

from src.prompt import (
    _build_address_validation_section,
    _build_tool_narration_section,
    build_system_prompt,
)


# ----- EN-locale tests -----


def test_en_address_validation_rule_present():
    section = _build_address_validation_section("en")
    assert isinstance(section, str)
    assert "ADDRESS VALIDATION — CRITICAL RULE" in section


def test_en_prohibited_phrases_enumerated():
    section = _build_address_validation_section("en")
    prohibited = [
        "validated",
        "verified",
        "confirmed against Google",
        "found your address",
        "looked up your address",
        "matches our records",
    ]
    hits = sum(1 for p in prohibited if p in section)
    assert hits >= 5, (
        f"EN missing prohibited phrases — got {hits}/6 hits; "
        f"expected at least 5. Section: {section!r}"
    )


def test_en_unless_clause_present():
    section = _build_address_validation_section("en")
    # Tool-return verdict tokens are code identifiers — MUST NOT be translated.
    assert "verdict=validated" in section
    assert "verdict=validated_with_corrections" in section


def test_en_position_in_top_attention_zone():
    # The full assembled prompt must place the address-validation rule BEFORE
    # the tool_narration section's output (top-attention zone alongside
    # outcome_words / call_duration).
    full = build_system_prompt(locale="en", business_name="Voco")
    addr_section = _build_address_validation_section("en")
    tool_narration = _build_tool_narration_section("en")
    addr_idx = full.find(addr_section)
    tool_idx = full.find(tool_narration)
    assert addr_idx >= 0, "Address validation section not assembled into EN prompt"
    assert tool_idx >= 0, "Tool narration section not assembled into EN prompt"
    assert addr_idx < tool_idx, (
        f"Address validation section must precede tool narration "
        f"(addr_idx={addr_idx}, tool_idx={tool_idx})"
    )


# ----- ES-locale tests -----


def test_es_address_validation_rule_present():
    section = _build_address_validation_section("es")
    assert isinstance(section, str)
    assert "VALIDACIÓN DE DIRECCIÓN — REGLA CRÍTICA" in section


def test_es_prohibited_phrases_enumerated():
    section = _build_address_validation_section("es")
    # Spanish prohibited phrases (planner-pinned). Test counts unique
    # base lemmas — `validado` and `validada` both count as "validado*".
    prohibited_groups = [
        ("validado", "validada"),
        ("verificado", "verificada"),
        ("confirmado contra Google",),
        ("encontré su dirección",),
        ("consulté su dirección",),
        ("coincide con nuestros registros",),
    ]
    hits = sum(1 for group in prohibited_groups if any(p in section for p in group))
    assert hits >= 4, (
        f"ES missing prohibited phrases — got {hits}/6 group-hits; "
        f"expected at least 4. Section: {section!r}"
    )


def test_es_unless_clause_present():
    section = _build_address_validation_section("es")
    # Verdict tokens are code identifiers — same in both locales.
    assert "verdict=validated" in section
    assert "verdict=validated_with_corrections" in section


def test_es_position_in_top_attention_zone():
    full = build_system_prompt(locale="es", business_name="Voco")
    addr_section = _build_address_validation_section("es")
    tool_narration = _build_tool_narration_section("es")
    addr_idx = full.find(addr_section)
    tool_idx = full.find(tool_narration)
    assert addr_idx >= 0, "Address validation section not assembled into ES prompt"
    assert tool_idx >= 0, "Tool narration section not assembled into ES prompt"
    assert addr_idx < tool_idx, (
        f"Address validation section must precede tool narration "
        f"(addr_idx={addr_idx}, tool_idx={tool_idx})"
    )


# ----- Cross-locale parity guard -----


def test_en_es_distinct():
    en = _build_address_validation_section("en")
    es = _build_address_validation_section("es")
    assert en != es
    # Sanity: ES must contain Spanish text, not be an EN fallback.
    assert "REGLA CRÍTICA" in es


def test_both_locales_silence_acceptable():
    en = _build_address_validation_section("en").lower()
    es = _build_address_validation_section("es").lower()
    # Caller-readback as last resort; silence is always acceptable; false
    # confirmation is the worst failure mode. Both locales must surface this.
    assert "silence" in en or "neutral" in en
    assert "silencio" in es or "neutral" in es
