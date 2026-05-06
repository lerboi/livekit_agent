"""Phase 61.2 invariants — static guards against regression on Fixes A/B/C.

Pattern: open the source file as text, assert / refute substring presence.
No SDK imports, no mocking, no fixtures. Mirrors tests/test_no_generate_reply_in_src.py.

Source-of-truth: 61.2-CONTEXT.md (REVISED) D-A-01 / D-B-01 / D-C-01.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
TOOLS = SRC / "tools"

# Tools that MUST call mute_input_during_tool — caller-input is detached for the
# duration of the BLOCKING Gemini-Live tool wait so server VAD can't fire mid-tool
# and cancel the generation.
#
# Note: book_appointment.py is intentionally EXCLUDED. Fix A (Plan 02) extended
# the mute pattern to data-fetch tools only (check_caller_history, check_customer_account,
# capture_lead). book_appointment.py performs synchronous DB writes and does not
# use the Gemini-Live blocking-wait pattern that the mute helper protects against.
DATA_FETCH_TOOLS = [
    # Plan 02 additions (D-A-01):
    "check_caller_history.py",
    "check_customer_account.py",
    "capture_lead.py",
    # Pre-existing mute users (regression guard against accidental removal):
    "check_day.py",
    "check_slot.py",
    "next_available_days.py",
]

TERMINAL_TOOLS = [
    "transfer_call.py",
    "end_call.py",
]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_data_fetch_tools_mute():
    """D-A-01 + regression guard: every data-fetch tool calls mute_input_during_tool."""
    for fname in DATA_FETCH_TOOLS:
        path = TOOLS / fname
        assert path.exists(), f"missing tool file: {fname}"
        text = _read(path)
        assert "mute_input_during_tool" in text, (
            f"{fname} must call mute_input_during_tool — required by D-A-01"
        )


def test_terminal_tools_do_not_mute():
    """D-A-01 negative: terminal tools must NOT mute (they end the session intentionally)."""
    for fname in TERMINAL_TOOLS:
        path = TOOLS / fname
        assert path.exists(), f"missing tool file: {fname}"
        text = _read(path)
        assert "mute_input_during_tool" not in text, (
            f"{fname} must NOT call mute_input_during_tool — required by D-A-01"
        )


def test_unmute_fallback_at_least_25s():
    """D-B-01: _TOOL_MUTE_FALLBACK_S >= 25.0 — booking readback + recovery margin."""
    text = _read(TOOLS / "_availability_lib.py")
    # Tolerate 25.0, 30.0, etc — guard the FLOOR not the exact value.
    import re
    m = re.search(r"^_TOOL_MUTE_FALLBACK_S\s*=\s*([0-9.]+)", text, re.MULTILINE)
    assert m, "_TOOL_MUTE_FALLBACK_S constant not found in _availability_lib.py"
    value = float(m.group(1))
    assert value >= 25.0, (
        f"_TOOL_MUTE_FALLBACK_S = {value} — must be >= 25.0 per D-B-01"
    )


def test_function_tools_executed_listener():
    """D-B-01: mute_input_during_tool subscribes to function_tools_executed."""
    text = _read(TOOLS / "_availability_lib.py")
    assert "function_tools_executed" in text, (
        "_availability_lib.py must subscribe to function_tools_executed — D-B-01"
    )


def test_server_cancel_handler_installed():
    """D-C-01: agent.py installs _ServerCancelHandler on the google plugin loggers
    and writes both counter fields onto _diag_record."""
    text = _read(SRC / "agent.py")
    assert "class _ServerCancelHandler" in text, (
        "agent.py must define _ServerCancelHandler — D-C-01"
    )
    assert "livekit.plugins.google.realtime" in text, (
        "agent.py must attach handler to livekit.plugins.google.realtime logger — D-C-01"
    )
    assert "server_tool_cancellations" in text, (
        "agent.py must write server_tool_cancellations to _diag_record — D-C-01"
    )
    assert "orphaned_server_content" in text, (
        "agent.py must write orphaned_server_content to _diag_record — D-C-01"
    )
