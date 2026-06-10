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
    # 2026-06-11 single-prompt collapse: locale="es" returns the same English
    # body — invariant (rule present for es-locale calls) maps to EN header.
    section = _build_address_validation_section("es")
    assert isinstance(section, str)
    assert "ADDRESS VALIDATION — CRITICAL RULE" in section


def test_es_prohibited_phrases_enumerated():
    # 2026-06-11 collapse: the Spanish prohibited forms moved from the
    # removed ES branch to the LANGUAGE section's Spanish delivery guide —
    # same protection (a Spanish-speaking turn may not fabricate "validado"),
    # asserted against the assembled es-locale prompt.
    full = build_system_prompt(locale="es", business_name="Voco")
    prohibited_groups = [
        ("validado", "validada"),
        ("verificado", "verificada"),
        ("confirmado contra Google",),
        ("encontré su dirección",),
        ("consulté su dirección",),
        ("coincide con nuestros registros",),
    ]
    hits = sum(1 for group in prohibited_groups if any(p in full for p in group))
    assert hits >= 4, (
        f"Spanish prohibited phrases missing from assembled es prompt — got "
        f"{hits}/6 group-hits; expected at least 4."
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


def test_en_es_identical():
    # 2026-06-11 collapse: the old distinctness guard inverts — this section
    # must NOT fork on locale anymore.
    en = _build_address_validation_section("en")
    es = _build_address_validation_section("es")
    assert en == es


def test_both_locales_pre_tool_readback_explicit():
    """Phase 61.1: post-tool gating + a readback license must be explicit.

    The Phase 61 prompt deadlocked the pre-tool readback step because the rule
    read as if it governed the entire address conversation AND silence was
    explicitly licensed. The fix scopes the rule to post-tool speech and
    explicitly licenses a caller-words readback.

    2026-06-10 (early validation): the exact gating-sentence pin changed
    because validate_address joined the gated tool set — the sentence now
    reads "After validate_address, book_appointment, or capture_lead
    returns". The readback license moved from a pre-tool readback to the
    validate_address confirmation (address_ok/address_corrected directives)
    plus the address_noted caller-words readback — the "read back what the
    caller said" license is still asserted verbatim. The protected
    invariants are unchanged: post-tool gating is explicit, a readback is
    licensed, and NO silence license exists anywhere in the block.
    """
    # 2026-06-11 collapse: locale="es" returns the same EN body — the ES
    # string pins map to the EN pins applied to BOTH locale outputs.
    for locale in ("en", "es"):
        section = _build_address_validation_section(locale)
        # Post-tool gating must be EXPLICIT (covering the early
        # validate_address tool as well).
        assert "After validate_address, book_appointment, or capture_lead returns" in section
        # A caller-words readback must be EXPLICITLY licensed (the
        # address_noted branch — never leaves the model with nothing it is
        # allowed to say about the address).
        assert "read back what the caller said" in section
        # The silence escape hatch and unqualified worst-failure framing must be GONE.
        assert "Silence or a neutral readback is always acceptable" not in section
        assert "worst failure mode in this section" not in section


def test_both_locales_early_validation_flow():
    """2026-06-10: the rule must encode the early-validation flow — call
    validate_address the moment the address is given (after a one-sentence
    filler), speak the result once, cap at one correction loop, never read
    the address more than twice, and booking does not re-read a validated
    address."""
    # 2026-06-11 collapse: locale="es" returns the same EN body — the ES
    # twice-cap / no-silence pins map to the EN pins on BOTH locale outputs.
    for locale in ("en", "es"):
        section = _build_address_validation_section(locale)
        assert "validate_address" in section
        # All four STATE branches of the new tool are taught.
        for state in (
            "STATE:address_ok",
            "STATE:address_corrected",
            "STATE:address_unclear",
            "STATE:address_noted",
        ):
            assert state in section, f"missing {state!r}"
        # The read-at-most-twice cap.
        assert "more than twice" in section
        # No silence license anywhere (Phase 61.1 deadlock class) — the
        # filler before validate_address must be REQUIRED, not optional.
        assert "silence is acceptable" not in section.lower()
        assert "never leave the line silent" in section
