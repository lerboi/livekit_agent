"""Phase 62 invariants — pre-session caller_history fetch + system-prompt
injection (call AJ_bFP3MLdqnKqT, 2026-05-07).

Locks the move from eager-tool-call (3-5s first-turn silent gap) to
pre-session pre-fetch (parallelized with customer_context, finishes
during greeting playout — zero caller-perceived latency).

Static-grep guards across check_caller_history.py, prompt.py, and
agent.py. Plus pure-function tests of the fetch/format helpers using
no SDK or DB — substring-matching the produced STATE strings.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.prompt import _build_caller_history_section, build_system_prompt
from src.tools.check_caller_history import (
    fetch_caller_history,
    format_caller_history_state,
)

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
TOOLS = SRC / "tools"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ─── format_caller_history_state — pure function ──────────────────────────


def test_format_state_none_is_history_lookup_failed():
    """None input → history_lookup_failed STATE (DB unreachable).
    Same string the tool returned pre-refactor; preserves the existing
    DIRECTIVE so prompt-side handling is unchanged."""
    out = format_caller_history_state(None)
    assert "STATE:history_lookup_failed" in out
    assert "DIRECTIVE:" in out


def test_format_state_empty_dict_is_first_time_caller():
    """Empty dict {} marker → first_time_caller STATE. Distinguishable
    from None (which is fetch failure) — first-time caller is a
    successful fetch with no rows."""
    out = format_caller_history_state({})
    assert "STATE:first_time_caller" in out
    assert "DIRECTIVE:" in out


def test_format_state_repeat_caller_renders_summary():
    """Populated dict → STATE:repeat_caller with summary block."""
    history = {
        "customer": {"id": "cust-1", "name": "Leroy"},
        "appointments": [],
        "interactions": [
            {"kind": "inquiry", "job_type": "plumbing", "status": "open",
             "created_at": "2026-04-01T00:00:00Z"},
        ],
        "tenant_timezone": "Asia/Singapore",
    }
    out = format_caller_history_state(history)
    assert "STATE:repeat_caller" in out
    assert "prior_appointments=0" in out
    assert "prior_interactions=1" in out
    assert "Previous interactions (Leroy)" in out
    assert "plumbing" in out
    assert "DIRECTIVE:" in out


def test_format_state_directive_forbids_recitation():
    """The DIRECTIVE must explicitly forbid reciting prior history —
    the privacy invariant the tool's directive carried since Plan 12."""
    history = {
        "customer": {"id": "x", "name": "A"},
        "appointments": [],
        "interactions": [
            {"kind": "inquiry", "job_type": "X", "status": "open",
             "created_at": "2026-04-01T00:00:00Z"},
        ],
        "tenant_timezone": "UTC",
    }
    out = format_caller_history_state(history)
    assert "do not recite the caller's history" in out


# ─── _build_caller_history_section — system-prompt injection ──────────────


def test_section_omits_block_when_none():
    """None → empty string. The build_system_prompt sections list
    filters empty strings, so the prompt has no caller-history
    region at all when the fetch failed."""
    assert _build_caller_history_section(None) == ""


def test_section_omits_block_when_first_time_caller():
    """{} → empty string. First-time callers get the natural intake
    flow with no extra context block — matches the directive of
    'do not mention they're new'."""
    assert _build_caller_history_section({}) == ""


def test_section_renders_block_when_repeat_caller():
    """Populated history → 'CALLER HISTORY' header + STATE block."""
    history = {
        "customer": {"id": "x", "name": "Leroy"},
        "appointments": [],
        "interactions": [
            {"kind": "inquiry", "job_type": "plumbing", "status": "open",
             "created_at": "2026-04-01T00:00:00Z"},
        ],
        "tenant_timezone": "UTC",
    }
    out = _build_caller_history_section(history)
    assert "CALLER HISTORY" in out
    assert "STATE:repeat_caller" in out


def test_full_system_prompt_includes_caller_history_block():
    """End-to-end: build_system_prompt(caller_history=dict) emits
    the section in the assembled prompt."""
    history = {
        "customer": {"id": "x", "name": "Leroy"},
        "appointments": [],
        "interactions": [
            {"kind": "inquiry", "job_type": "plumbing", "status": "open",
             "created_at": "2026-04-01T00:00:00Z"},
        ],
        "tenant_timezone": "UTC",
    }
    prompt = build_system_prompt(
        locale="en",
        business_name="Voco",
        onboarding_complete=True,
        caller_history=history,
    )
    assert "CALLER HISTORY" in prompt
    assert "STATE:repeat_caller" in prompt


def test_full_system_prompt_omits_caller_history_block_when_none():
    """No caller_history → no caller-history region in the prompt."""
    prompt = build_system_prompt(
        locale="en",
        business_name="Voco",
        onboarding_complete=True,
        caller_history=None,
    )
    assert "CALLER HISTORY" not in prompt
    assert "STATE:repeat_caller" not in prompt


# ─── Tool description — eager-invoke directive removed ────────────────────


def test_tool_description_no_longer_eager_invoke():
    """The tool's @function_tool(description=...) must NOT instruct the
    agent to invoke check_caller_history at the start of every call.
    That eager pattern caused the 3-5s first-turn silent gap.

    Scopes the check to the actual function_tool decorator block — the
    file's module docstring is allowed to reference the old phrase as
    historical context.
    """
    text = _read(TOOLS / "check_caller_history.py")
    # Isolate the @function_tool block (description=... ends with the
    # closing ), of the decorator). Conservative slice — start at the
    # decorator opener.
    start = text.find("@function_tool(")
    assert start != -1, "check_caller_history.py must define @function_tool"
    end = text.find("async def check_caller_history(", start)
    assert end != -1, "check_caller_history.py must define the async tool fn"
    decorator_block = text[start:end]

    forbidden = (
        "Invoke after greeting, before first question",
        "before first question",
    )
    for f in forbidden:
        assert f not in decorator_block, (
            f"check_caller_history tool description must not carry the "
            f"eager-invoke directive {f!r} — Phase 62 (call "
            f"AJ_bFP3MLdqnKqT regression)"
        )


def test_tool_description_directs_to_explicit_caller_request():
    """The new directive must scope the tool to explicit caller asks
    so the agent doesn't fall back to eager invocation by habit."""
    text = _read(TOOLS / "check_caller_history.py")
    assert "explicitly asks" in text, (
        "tool description must scope invocation to explicit caller request"
    )
    assert "do NOT need to invoke this at call start" in text or \
           "already provided in your initial context" in text, (
        "tool description must signal that history is pre-injected"
    )


# ─── agent.py — pre-session fetch wired ───────────────────────────────────


def test_agent_pre_fetches_caller_history():
    """agent.py must call fetch_caller_history pre-session, in
    parallel with the customer_context fetch, and pass the result to
    build_system_prompt."""
    text = _read(SRC / "agent.py")
    assert "fetch_caller_history" in text, (
        "agent.py must import and call fetch_caller_history pre-session"
    )
    assert "caller_history=caller_history" in text, (
        "agent.py must pass caller_history kwarg to build_system_prompt"
    )
    # Parallel with customer_context — asyncio.gather signature.
    assert "asyncio.gather(" in text, (
        "agent.py should run customer_context + caller_history in parallel"
    )


def test_agent_exposes_caller_history_on_deps():
    """deps['caller_history'] must be set so any future mid-call code
    path can read the pre-fetched data without re-querying."""
    text = _read(SRC / "agent.py")
    assert '"caller_history": caller_history' in text, (
        "agent.py deps must include caller_history"
    )


# ─── fetch_caller_history fail-soft ───────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_returns_none_on_missing_args():
    """fetch_caller_history with empty tenant_id or from_number must
    return None — the entrypoint guards against passing into Supabase
    with empty filters."""
    out = await fetch_caller_history(None, "", "+15551234567")
    assert out is None
    out2 = await fetch_caller_history(None, "tenant-x", "")
    assert out2 is None
