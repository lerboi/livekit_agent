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
3. test_en_instructs_three_second_target — EN contains "~3 seconds" OR
   "3 seconds" (duration target preserved).

ES:
4. test_es_exists_and_nonempty — ES returns string >200 chars.
5. test_es_does_not_claim_runtime_filler — ES does NOT contain
   "runtime" or "session.say" — same invariant translated.
6. test_es_instructs_filler_before_tool — ES contains "nunca" AND
   ("herramienta" OR "tool") AND "hablar"/"habla" (filler-before-tool
   contract in Spanish).
7. test_es_mentions_tool_names — ES contains the exact strings
   "check_availability", "book_appointment", "capture_lead",
   "transfer_call" (tool names are code identifiers, never translated).

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


def test_en_instructs_three_second_target():
    """EN branch keeps the ~3-second filler duration target (Rule 3)."""
    section = _build_tool_narration_section("en")
    # Either "~3 seconds" or "3 seconds" acceptable.
    assert ("~3 seconds" in section) or ("3 seconds" in section)


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
    """ES branch must contain the filler-before-tool contract in Spanish:
    a negated 'nunca' clause + a clear speak-first instruction."""
    section = _build_tool_narration_section("es")
    lowered = section.lower()
    # Anti-silent-tool-call negation must stay negated (anti-hallucination
    # NEVER guard — R-B5 A1 exception, per 60.3-PROMPT-AUDIT.md).
    assert "nunca" in lowered
    # The tool concept must be present (either Spanish "herramienta" or the
    # code identifier "tool" carried over unchanged).
    assert ("herramienta" in lowered) or ("tool" in lowered)
    # The speak-first instruction uses some form of "hablar" (to speak) or
    # "habla" (speak!) — both are acceptable imperative forms.
    assert ("hablar" in lowered) or ("habla" in lowered)


def test_es_mentions_tool_names():
    """Tool names are code identifiers — they must appear untranslated in
    the Spanish branch so the model wires filler examples to the actual
    tool registry."""
    section = _build_tool_narration_section("es")
    for tool in (
        "check_availability",
        "book_appointment",
        "capture_lead",
        "transfer_call",
    ):
        assert tool in section, f"tool name {tool!r} missing from ES branch"


# --- Parity ---


def test_en_and_es_are_distinct():
    """EN and ES branches must be different — guards against a copy-paste
    bug where locale='es' accidentally returns the EN body."""
    en = _build_tool_narration_section("en")
    es = _build_tool_narration_section("es")
    assert en != es
    # And ES must actually contain Spanish prose (not just a header).
    # A Spanish-specific word that has no EN cognate: "llamante" (the caller).
    assert "llamante" in es.lower()
