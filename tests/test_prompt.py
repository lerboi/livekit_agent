"""Phase 60.2 post-mortem — assert _build_tool_narration_section() does NOT
claim a runtime filler.

Fix H (runtime filler via context.session.say) was reverted because it cannot
produce audio on a RealtimeModel-only AgentSession in livekit-agents 1.5.1
(session.say requires a TTS, which is not attached when the session is wired
as AgentSession(llm=RealtimeModel)). The narration section must instruct the
model to speak its own filler — if it ever claims a runtime filler again,
Gemini will go silent during tool execution and the race returns.
"""
from __future__ import annotations

from src.prompt import _build_tool_narration_section


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
