"""Phase 61.3 invariants — static guards for the cascade-recovery mechanism.

Pattern: open the source file as text, assert / refute substring presence.
No SDK imports, no mocking, no fixtures. Mirrors tests/test_tool_mute_invariants.py.

Source-of-truth: 61.3-CONTEXT.md D-03 (recovery location) / D-05 (replay API) /
D-06 (replay-before-unmute order) / D-08 (counter pattern + conditional emit) /
D-10 (this test layout).

These tests are RED until Plan 03 lands the _attempt_tool_result_replay helper.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
TOOLS = SRC / "tools"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_replay_path_in_fallback():
    """D-03: cascade-recovery helper exists and is invoked from
    the TimeoutError fallback branch of _unmute_logic."""
    text = _read(TOOLS / "_availability_lib.py")
    assert "_attempt_tool_result_replay" in text, (
        "_availability_lib.py must define _attempt_tool_result_replay — D-03"
    )
    # Helper must be invoked (not just defined). Look for an await on it.
    assert "await _attempt_tool_result_replay" in text, (
        "_attempt_tool_result_replay must be awaited inside _unmute_logic — D-03"
    )


def test_replay_uses_update_chat_ctx():
    """D-05: replay sends the synthetic FunctionCallOutput via update_chat_ctx —
    the only API path that bypasses the mutable_chat_context gate (realtime_api.py:637-638)."""
    text = _read(TOOLS / "_availability_lib.py")
    assert "update_chat_ctx" in text, (
        "_availability_lib.py must call update_chat_ctx for tool-result replay — D-05"
    )
    assert "FunctionCallOutput" in text, (
        "_availability_lib.py must construct a FunctionCallOutput for replay — D-05"
    )


def test_replay_not_generate_reply():
    """D-05: generate_reply() is gated for gemini-3.1-flash-live-preview
    (raises RealtimeError on 1.5.7); the replay MUST NOT use it."""
    text = _read(TOOLS / "_availability_lib.py")
    assert "generate_reply" not in text, (
        "_availability_lib.py must NOT call generate_reply — gated for 3.1, D-05"
    )


def test_replay_before_set_audio_enabled():
    """D-06: replay must fire BEFORE re-enabling input audio so the user's
    next utterance lands on the regenerated context, not the stale one."""
    text = _read(TOOLS / "_availability_lib.py")
    replay_idx = text.find("_attempt_tool_result_replay")
    unmute_idx = text.find("set_audio_enabled(True)")
    assert replay_idx != -1, (
        "replay helper must be present in _availability_lib.py — D-06"
    )
    assert unmute_idx != -1, (
        "set_audio_enabled(True) must be present in _availability_lib.py — D-06"
    )
    assert replay_idx < unmute_idx, (
        "replay invocation must appear before set_audio_enabled(True) in source order — D-06"
    )


def test_stall_recovery_counters_present():
    """D-08: two new diag counters declared in _availability_lib.py — the
    recovery-attempt counter and the recovery-failure counter."""
    text = _read(TOOLS / "_availability_lib.py")
    assert "stalled_generation_recoveries" in text, (
        "_availability_lib.py must increment stalled_generation_recoveries — D-08"
    )
    assert "stalled_generation_replay_failed" in text, (
        "_availability_lib.py must increment stalled_generation_replay_failed — D-08"
    )


def test_stall_counters_conditional_emit():
    """D-08: counters use the .get(key, 0) + 1 pattern so the keys only
    appear in [goodbye_race] JSON when count > 0 (matches _ServerCancelHandler)."""
    text = _read(TOOLS / "_availability_lib.py")
    # Both counters must use the conditional-emit pattern.
    assert '.get("stalled_generation_recoveries", 0)' in text, (
        "stalled_generation_recoveries must use .get(key, 0) + 1 conditional-emit pattern — D-08"
    )
    assert '.get("stalled_generation_replay_failed", 0)' in text, (
        "stalled_generation_replay_failed must use .get(key, 0) + 1 conditional-emit pattern — D-08"
    )
