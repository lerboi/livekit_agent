"""Phase 60.2 — assert _build_tool_narration_section() reflects runtime filler."""
from __future__ import annotations

from src.prompt import _build_tool_narration_section


def test_tool_narration_mentions_runtime_filler():
    """Plan 04 must add a sentence instructing the model NOT to speak its own filler."""
    section = _build_tool_narration_section()
    assert isinstance(section, str)
    lowered = section.lower()
    # Either of these substrings is acceptable wording — Plan 04 picks one.
    runtime_claim = "runtime" in lowered or "automatically" in lowered
    do_not_speak = "do not speak" in lowered or "don't speak" in lowered or "do not generate" in lowered
    assert runtime_claim, (
        "narration must note the tool runtime plays a filler automatically"
    )
    assert do_not_speak, (
        "narration must instruct the model NOT to speak its own filler phrase"
    )
