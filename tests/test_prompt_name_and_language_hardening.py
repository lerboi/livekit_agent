"""Phase 62 invariants — name-use forbidden-patterns + language false-flip
prompt hardening (call AJ_b8ACLgXZ4XZA, AJ_gpRzniyNoJBd from 2026-05-07).

These tests are static-grep guards over the assembled prompt sections.
They lock in the rewrites that close two production UX failures observed
on call AJ_gpRzniyNoJBd (Make It AI tenant 24141cd0):

1. Name repetition mid-call: agent said "Thank you, Dior." and "Dior, I have…"
   despite an existing rule forbidding mid-call name use. Fix added an
   explicit forbidden-patterns enumeration plus an outcome-based
   acknowledgment rule with no name use.

2. Language false-flip: STT misclassified English audio as German; the agent
   replied "I'm sorry, I only speak English." Fix reframed ANTI-HALLUCINATION
   from "if you cannot understand" (predicate failed because Gemini was
   confident) to "if your transcription appears in a non-supported
   language, treat as STT error of English" (predicate matches the actual
   failure mode).

No SDK imports, no mocks, no fixtures — pure substring greps. Same pattern
as test_cascade_recovery_invariants.py.
"""
from __future__ import annotations

from src.prompt import _build_info_gathering_section, _build_language_section


def _noop_t(key: str) -> str:
    return key


# ─── Name-use forbidden-patterns + outcome-based acknowledgment ──────────


def test_en_name_use_forbidden_patterns_block_present():
    """English info-gathering section must enumerate the forbidden vocative
    patterns Gemini violated on call AJ_gpRzniyNoJBd."""
    section = _build_info_gathering_section(_noop_t, "postal code", "en")
    assert "Forbidden patterns" in section, (
        "EN info-gathering must enumerate forbidden name-use patterns"
    )
    # Specific anti-patterns from the AJ_gpRzniyNoJBd transcript and
    # nearby siblings — Gemini pattern-matches against these directly.
    for pattern in ("Thanks, {name}", "Thank you, {name}", "{name}, I have"):
        assert pattern in section, (
            f"EN forbidden-patterns block must include {pattern!r}"
        )


def test_es_name_use_forbidden_patterns_block_present():
    """2026-06-11 single-prompt collapse: locale="es" returns the same EN
    body — the invariant (forbidden vocative patterns enumerated for
    es-locale calls) maps to the EN enumeration."""
    section = _build_info_gathering_section(_noop_t, "código postal", "es")
    assert "Forbidden patterns" in section, (
        "es-locale info-gathering must enumerate forbidden name-use patterns"
    )
    for pattern in ("Thanks, {name}", "{name}, I have"):
        assert pattern in section, (
            f"es-locale forbidden-patterns block must include {pattern!r}"
        )


def test_en_acknowledgment_outcome_no_name():
    """EN must instruct the model to acknowledge without using the caller's
    name (outcome-based per feedback_livekit_prompt_philosophy)."""
    section = _build_info_gathering_section(_noop_t, "postal code", "en")
    assert "must not contain the caller's name" in section, (
        "EN must explicitly state acknowledgment cannot contain the name"
    )


def test_es_acknowledgment_outcome_no_name():
    """2026-06-11 collapse: same invariant for es-locale calls, EN pin."""
    section = _build_info_gathering_section(_noop_t, "código postal", "es")
    assert "must not contain the caller's name" in section, (
        "es-locale output must state acknowledgment cannot contain the name"
    )


def test_en_sole_on_air_name_moment_is_booking_readback():
    """The booking readback is the single on-air name moment. Both prior
    rule and new rule keep this — the test locks the framing so future
    edits can't regress to mid-call name use without flagging."""
    section = _build_info_gathering_section(_noop_t, "postal code", "en")
    assert "SOLE moment" in section or "sole moment" in section.lower(), (
        "EN must frame booking readback as the SOLE on-air name moment"
    )


def test_es_sole_on_air_name_moment_is_booking_readback():
    # 2026-06-11 collapse: same invariant for es-locale calls, EN pin.
    section = _build_info_gathering_section(_noop_t, "código postal", "es")
    assert "SOLE moment" in section or "sole moment" in section.lower(), (
        "es-locale output must frame booking readback as the SOLE name moment"
    )


# ─── Language false-flip — STT-error reframing ───────────────────────────


def test_en_anti_hallucination_reframed_to_stt_error():
    """EN ANTI-HALLUCINATION must trigger on transcription-language
    mismatch, not just unintelligible audio. The original predicate
    ("if you cannot understand") missed the AJ_gpRzniyNoJBd failure
    where Gemini was confident the German transcript was correct.

    2026-06-11 collapse: pin widened from "STT errors of English audio" to
    "STT errors of English or Spanish audio" — the unified section merged
    the removed ES branch's Spanish-audio-misheard semantic; the protected
    invariant (language mismatch = STT error, not a switch) is unchanged."""
    section = _build_language_section(_noop_t, "en")
    assert "STT errors of English or Spanish audio" in section, (
        "anti-hallucination must classify transcription-language "
        "mismatch as an STT error of supported-language audio"
    )
    # The list of unsupported languages the STT may misclassify audio as.
    for lang in ("German", "French", "Italian"):
        assert lang in section, (
            f"anti-hallucination must list {lang} as an example "
            "language that almost always indicates a STT error"
        )


def test_es_anti_hallucination_reframed_to_stt_error():
    # 2026-06-11 collapse: es-locale output is the same English text — same
    # invariant, EN pins.
    section = _build_language_section(_noop_t, "es")
    assert "STT errors of English or Spanish audio" in section, (
        "es-locale anti-hallucination must classify transcription-language "
        "mismatch as an STT error of supported-language audio"
    )
    for lang in ("German", "French", "Italian"):
        assert lang in section, (
            f"es-locale anti-hallucination must list {lang} as an example "
            "misheard-language"
        )


def test_en_forbids_self_disclosing_only_speaks_english():
    """The agent must NOT tell the caller it only speaks English in
    response to a non-English transcript — this reveals the STT
    failure to the caller and damages trust. Verbatim from the
    AJ_gpRzniyNoJBd transcript: 'I'm sorry, I only speak English.'"""
    section = _build_language_section(_noop_t, "en")
    assert "do NOT tell the caller you only speak English" in section, (
        "EN must explicitly forbid 'I only speak English' as a reply"
    )


def test_es_forbids_self_disclosing_only_speaks_spanish():
    # 2026-06-11 collapse: same self-disclosure ban for es-locale calls; the
    # es-locale output is the same English text — EN pin.
    section = _build_language_section(_noop_t, "es")
    assert "do NOT tell the caller you only speak" in section, (
        "es-locale output must forbid disclosing the language limitation"
    )


def test_en_explicit_switch_phrase_required():
    """A real language switch requires the caller to explicitly request
    one. Mere appearance of foreign text in the transcript is not
    consent. Lock this so the rule can't drift back to auto-detect."""
    section = _build_language_section(_noop_t, "en")
    assert "NOT consent to switch" in section, (
        "EN must clarify that foreign text in transcript is NOT consent "
        "to switch languages"
    )


def test_es_explicit_switch_phrase_required():
    # 2026-06-11 collapse: same invariant for es-locale calls, EN pin (the
    # unified text also carries the "¿Podemos hablar en inglés?" example
    # harvested from the removed ES branch).
    section = _build_language_section(_noop_t, "es")
    assert "NOT consent to switch" in section, (
        "es-locale output must clarify that foreign text in transcript is "
        "NOT consent to switch languages"
    )
    assert "¿Podemos hablar en inglés?" in section


def test_en_connection_issue_framing_for_repeat_request():
    """When the model needs to ask the caller to repeat, frame it as a
    connection issue, not a language barrier. Avoids triggering the
    same caller-confidence damage as 'I only speak English.'"""
    section = _build_language_section(_noop_t, "en")
    # The substitution phrase from the new wording — locks the example.
    assert "audio cut out" in section, (
        "EN must offer a connection-issue substitution phrase"
    )


def test_es_connection_issue_framing_for_repeat_request():
    # 2026-06-11 collapse: same invariant for es-locale calls, EN pin.
    section = _build_language_section(_noop_t, "es")
    assert "audio cut out" in section, (
        "es-locale output must offer a connection-issue substitution phrase"
    )
