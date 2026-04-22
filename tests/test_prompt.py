"""Phase 60.2 post-mortem — assert _build_tool_narration_section() does NOT
claim a runtime filler.

Fix H (runtime filler via context.session.say) was reverted because it cannot
produce audio on a RealtimeModel-only AgentSession in livekit-agents 1.5.1
(session.say requires a TTS, which is not attached when the session is wired
as AgentSession(llm=RealtimeModel)). The narration section must instruct the
model to speak its own filler — if it ever claims a runtime filler again,
Gemini will go silent during tool execution and the race returns.

Phase 60.3 Stream A Plan 3 (Branch P) additions — call_duration CRITICAL RULE:

UAT #1 evidence (60.3-HUMAN-UAT.md, call-_+6587528516_KwsBVWBZkKps) showed
Gemini invoked end_call during the goodbye turn rather than waiting for
silence (end_call_invoked_at fired 11ms BEFORE last_text_token_at, and the
caller heard "Alright, I'll get all" truncated mid-sentence). The UAT
evidence also matches the upstream livekit/agents #5096 _SegmentSynchronizerImpl
signature (text_done=false, audio_done=true) — the pipeline-sync race is
systemic.

Per Plan 2 ambiguity-resolution rule, Branch P (prompt-hardening) ships first:
promote _build_call_duration_section to a CRITICAL RULE block with an explicit
failure-mode example showing both the unsafe pattern (goodbye + end_call same
turn) and the correct pattern (goodbye → silence → end_call separate turn).
The section is reordered to position 5 in build_system_prompt (immediately
after _build_outcome_words_section, before _build_tool_narration_section) so
Gemini attends more strongly to it (RESEARCH §R-B5: top-of-prompt attention).

Invariants asserted here:
1. _build_call_duration_section returns the literal substring
   "ENDING THE CALL — CRITICAL RULE:" (case-sensitive).
2. The returned block contains a concrete failure-mode example with both the
   WRONG and RIGHT patterns.
3. When assembled via build_system_prompt, the call_duration block appears
   BEFORE the tool_narration block.
4. The 9-minute and 10-minute duration bounds are preserved (existing
   behavior must not regress).
5. The section MUST NOT reintroduce the 60.2 Fix H "session.say"/"runtime
   plays" pattern (Pitfall 6 — reverted for RealtimeModel-only sessions).
"""
from __future__ import annotations

from src.prompt import (
    _build_call_duration_section,
    _build_tool_narration_section,
    build_system_prompt,
)


def test_tool_narration_does_not_claim_runtime_filler():
    section = _build_tool_narration_section()
    assert isinstance(section, str)
    lowered = section.lower()
    assert "runtime automatically plays" not in lowered
    assert "runtime filler" not in lowered
    assert "do not speak your own filler" not in lowered
    assert "do not generate your own filler" not in lowered


def test_tool_narration_instructs_model_to_speak_filler():
    section = _build_tool_narration_section()
    lowered = section.lower()
    # The model IS the filler source — ensure the rule is present.
    assert "speak" in lowered and "filler" in lowered
    assert "never emit a tool call without speaking" in lowered


# --- Phase 60.3 Stream A Plan 3 (Branch P) — call_duration CRITICAL RULE ---


def _t_stub(key: str) -> str:
    """Minimal t() stub — _build_call_duration_section currently does not use t."""
    return key


def test_call_duration_is_critical_rule_framed():
    """Invariant 1: the section header is a CRITICAL RULE block."""
    section = _build_call_duration_section(_t_stub)
    assert isinstance(section, str)
    # Case-sensitive — the word "CRITICAL RULE" must appear in that exact form
    # so the top-attention-band framing is consistent with other CRITICAL RULE
    # sections (OUTCOME WORDS, TOOL NARRATION, CORRECTIONS, CUSTOMER CONTEXT).
    assert "ENDING THE CALL — CRITICAL RULE:" in section


def test_call_duration_has_failure_mode_example():
    """Invariant 2: the section contains a concrete failure-mode example
    showing both the unsafe pattern (goodbye + end_call same turn) and the
    correct pattern (goodbye → silence → end_call separate turn)."""
    section = _build_call_duration_section(_t_stub)
    lowered = section.lower()
    # Anchor the example block — either "failure mode" framing or a WRONG:
    # anti-pattern marker must be present.
    assert ("failure mode" in lowered) or ("wrong:" in lowered)
    # Both sides of the example must be shown: the mid-sentence cutoff
    # (wrong) AND the separate-turn end_call invocation (right).
    # The WRONG path shows end_call invoked mid-farewell.
    assert "end_call" in section
    # The RIGHT path must describe silence/pause before end_call in a
    # separate turn. Match any of the explicit markers the rewrite uses.
    assert any(
        marker in section
        for marker in ("silence", "SILENCE", "separate turn", "separate step")
    ), "failure-mode example must show the correct path (silence/separate turn)"
    # The example must show both unsafe and correct patterns in the same
    # block — check for explicit WRONG and RIGHT markers (or equivalent).
    assert "WRONG" in section or "Failure mode" in section
    assert "RIGHT" in section or "Correct path" in section


def test_call_duration_moved_above_tool_narration():
    """Invariant 3: in the assembled prompt, call_duration appears BEFORE
    tool_narration (top-attention-band placement)."""
    assembled = build_system_prompt(
        locale="en",
        business_name="Voco",
        onboarding_complete=True,
    )
    idx_call_duration = assembled.index("ENDING THE CALL — CRITICAL RULE")
    idx_tool_narration = assembled.index("TOOL NARRATION — CRITICAL RULE")
    assert idx_call_duration < idx_tool_narration, (
        "call_duration CRITICAL RULE must appear before tool_narration in the "
        "assembled prompt (top-attention-band placement — RESEARCH §R-B5)"
    )


def test_call_duration_preserves_9_and_10_minute_bounds():
    """Invariant 4: the existing 9-minute wrap-up and 10-minute hard-max
    behavior must not regress — keep "9 minutes" and "10 minutes" literals."""
    section = _build_call_duration_section(_t_stub)
    assert "9 minutes" in section
    assert "10 minutes" in section


def test_call_duration_does_not_fabricate_session_say():
    """Invariant 5 (Pitfall 6 guard): the section MUST NOT reintroduce the
    60.2 Fix H "session.say"/"runtime plays" pattern — session.say cannot
    produce audio on a RealtimeModel-only AgentSession in livekit-agents 1.5.1."""
    section = _build_call_duration_section(_t_stub)
    lowered = section.lower()
    assert "session.say" not in lowered
    assert "runtime plays" not in lowered
