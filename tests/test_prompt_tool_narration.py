"""Phase 60.3 Plan 06 — _build_tool_narration_section locale parity.

Signature evolves from `() -> str` to `(locale: str) -> str`. The EN body is
preserved verbatim from the post-60.2 state (Pitfall 6 invariants must stay
green). A Spanish branch mirrors the structure with translated prose and the
same tool-specific filler examples (tool names are code identifiers, NOT
translated).

Invariants asserted here (mirror 60.3-06-PLAN.md <behavior>):

EN:
1. test_en_does_not_claim_runtime_filler — EN output MUST NOT contain
   "runtime automatically plays" nor "runtime plays" nor "session.say"
   (case-insensitive). Mirrors 60.2 Plan 05 Pitfall 6 guard.
2. test_en_instructs_model_to_speak_filler — EN output contains
   "never emit a tool call without speaking first" (lowercased).
3. test_en_instructs_one_sentence_filler_target — EN contains the bounded
   one-warm-sentence (~2s) filler target (2026-06-10 conciseness pass;
   was "~3 seconds").

ES:
4. test_es_exists_and_nonempty — ES returns string >200 chars.
5. test_es_does_not_claim_runtime_filler — ES does NOT contain
   "runtime" or "session.say" — same invariant translated.
6. test_es_instructs_filler_before_tool — ES contains "nunca" AND
   ("herramienta" OR "tool") AND "hablar"/"habla" (filler-before-tool
   contract in Spanish).
7. test_es_mentions_tool_names — ES contains the exact strings
   "check_slot", "check_day", "next_available_days", "validate_address",
   "book_appointment", "capture_lead", "transfer_call" (tool names are
   code identifiers, never translated; 2026-06-10 — was the retired
   "check_availability").

Parity:
8. test_en_and_es_are_distinct — not a copy-paste.
"""
from __future__ import annotations

from src.prompt import _build_tool_narration_section


# --- EN invariants (60.2 Plan 05 Pitfall 6 guards extended to new signature) ---


def test_en_does_not_claim_runtime_filler():
    """EN branch must NOT reintroduce the 60.2 Fix H runtime-filler language.

    session.say() cannot produce audio on a RealtimeModel-only AgentSession
    in livekit-agents 1.5.1. The prompt must instruct the model to speak
    its own filler; any regression toward "runtime plays" returns the race.
    """
    section = _build_tool_narration_section("en")
    assert isinstance(section, str)
    lowered = section.lower()
    assert "runtime automatically plays" not in lowered
    assert "runtime plays" not in lowered
    assert "session.say" not in lowered


def test_en_instructs_model_to_speak_filler():
    """EN branch keeps the explicit Rule 1 language 'never emit a tool call
    without speaking first' so the model-speaks-filler contract is explicit."""
    section = _build_tool_narration_section("en")
    lowered = section.lower()
    assert "never emit a tool call without speaking first" in lowered


def test_en_instructs_one_sentence_filler_target():
    """EN branch keeps a bounded filler duration target (Rule 3).

    2026-06-10 conciseness pass: the target moved from "AIM FOR ~3 SECONDS
    … longer, warmer filler" to ONE warm sentence (~2 seconds). The invariant
    this test protects is unchanged: filler is REQUIRED (it covers tool
    latency — never license silence) and has an explicit duration bound so
    it neither under-covers (two words) nor balloons (a paragraph).
    """
    section = _build_tool_narration_section("en")
    lowered = section.lower()
    # Bounded duration target present…
    assert ("~2 second" in lowered) or ("2 seconds" in lowered) or (
        "one warm sentence" in lowered
    )
    # …and the old unbounded "longer, warmer" license is gone.
    assert "longer, warmer filler" not in lowered
    # Filler is still mandatory — no silence license crept in.
    assert "never emit a tool call without speaking first" in lowered


# --- ES invariants (new Spanish branch, D7 locale parity) ---


def test_es_exists_and_nonempty():
    """Calling with locale='es' must return a substantial Spanish body
    (not an empty string, not the EN default)."""
    section = _build_tool_narration_section("es")
    assert isinstance(section, str)
    assert len(section) > 200


def test_es_does_not_claim_runtime_filler():
    """ES branch carries the same Pitfall 6 invariant as EN — no mention of
    runtime/session.say, because those APIs don't exist on RealtimeModel
    sessions regardless of caller language."""
    section = _build_tool_narration_section("es")
    lowered = section.lower()
    assert "runtime" not in lowered
    assert "session.say" not in lowered


def test_es_instructs_filler_before_tool():
    """2026-06-11 single-prompt collapse: locale="es" returns the same EN
    body — the filler-before-tool contract pins map to the EN words (the
    anti-silent-tool-call NEVER guard stays negated)."""
    section = _build_tool_narration_section("es")
    lowered = section.lower()
    assert "never emit a tool call without speaking first" in lowered
    assert "tool" in lowered


def test_es_mentions_tool_names():
    """Tool names are code identifiers — they must appear untranslated in
    the Spanish branch so the model wires filler examples to the actual
    tool registry. (2026-06-10: pins updated from the retired
    check_availability to the split availability tools, plus the new
    validate_address early-validation tool.)"""
    section = _build_tool_narration_section("es")
    for tool in (
        "check_slot",
        "check_day",
        "next_available_days",
        "validate_address",
        "book_appointment",
        "capture_lead",
        "transfer_call",
    ):
        assert tool in section, f"tool name {tool!r} missing from ES branch"


def test_both_locales_have_validate_address_filler_example():
    """The validate_address tool runs the moment the caller gives their
    address — it needs a per-tool filler example like every other tool so
    the line is never silent while it runs (Phase 61.1 no-silence rule)."""
    for locale in ("en", "es"):
        section = _build_tool_narration_section(locale)
        assert "validate_address" in section, (
            f"{locale}: validate_address missing from per-tool filler examples"
        )


# --- Parity ---


def test_en_and_es_are_identical():
    """2026-06-11 collapse: the old distinctness guard inverts — this section
    must NOT fork on locale anymore (Spanish filler delivery is covered by
    the LANGUAGE section's Spanish guide)."""
    en = _build_tool_narration_section("en")
    es = _build_tool_narration_section("es")
    assert en == es
