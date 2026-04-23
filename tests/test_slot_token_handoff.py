"""Phase-fix tests (2026-04-24): slot_token structural handoff between
check_availability and book_appointment.

Background: live UAT call-_+6587528516_iv7QFp8tqKXC (2026-04-24) showed
Gemini 3.1 Flash Live IGNORING the "pass slot_start_utc VERBATIM"
directive. For a 2-PM-SGT booking Gemini constructed
`2026-04-27T14:00:00` (caller's wall-clock digits, no tz) instead of the
authoritative `2026-04-27T06:00:00+00:00`. _ensure_utc_iso coerced the
naive ISO to UTC, producing an off-by-8-hours booking (event landed at
10 PM SGT instead of 2 PM SGT, confirmed via Google Calendar UI).

Structural fix (this test file): check_availability stashes (token ->
UTC slot_start/end) on deps["_slot_tokens"]; book_appointment resolves
by token and IGNORES Gemini-supplied slot_start/slot_end when token is
valid.
"""
from __future__ import annotations

import time

import pytest

from src.tools.check_availability import _register_slot_token


def test_register_slot_token_shape():
    deps = {}
    token = _register_slot_token(
        deps,
        "2026-04-27T06:00:00+00:00",
        "2026-04-27T07:00:00+00:00",
    )
    assert token.startswith("slot_"), f"token must be prefixed; got {token!r}"
    assert len(token) == len("slot_") + 8, f"8-hex-char body; got {token!r}"
    assert "_slot_tokens" in deps
    entry = deps["_slot_tokens"][token]
    assert entry["slot_start_utc"] == "2026-04-27T06:00:00+00:00"
    assert entry["slot_end_utc"] == "2026-04-27T07:00:00+00:00"
    assert isinstance(entry["created_at"], float)
    assert (time.time() - entry["created_at"]) < 1.0


def test_register_slot_token_collision_resistant():
    """Registering 100 tokens back-to-back must not collide."""
    deps = {}
    tokens = [
        _register_slot_token(
            deps,
            f"2026-04-27T{h:02d}:00:00+00:00",
            f"2026-04-27T{h+1:02d}:00:00+00:00",
        )
        for h in range(10)
        for _ in range(10)
    ]
    assert len(tokens) == 100
    assert len(set(tokens)) == 100, "unexpected token collision"


def test_register_slot_token_multiple_in_single_call():
    """A single check_availability invocation can register 3 alternatives
    simultaneously without overwriting prior tokens (the alternatives
    branch of check_availability does this)."""
    deps = {}
    tok1 = _register_slot_token(deps, "A_start", "A_end")
    tok2 = _register_slot_token(deps, "B_start", "B_end")
    tok3 = _register_slot_token(deps, "C_start", "C_end")
    assert tok1 != tok2 != tok3
    assert deps["_slot_tokens"][tok1]["slot_start_utc"] == "A_start"
    assert deps["_slot_tokens"][tok2]["slot_start_utc"] == "B_start"
    assert deps["_slot_tokens"][tok3]["slot_start_utc"] == "C_start"


# ── book_appointment token resolution ───────────────────────────────────

def _extract_resolution_logic(deps: dict, slot_token: str,
                              gemini_start: str, gemini_end: str) -> tuple[str, str, bool]:
    """Mirror the resolution block at book_appointment.py L215-247.
    Keeping this as a pure-function shadow lets us unit-test the contract
    without instantiating the full @function_tool chain (which requires
    a RunContext + Supabase + tenant_id + etc).

    Returns (slot_start, slot_end, token_resolved).
    """
    _token_resolved = False
    if slot_token:
        _tokens = deps.get("_slot_tokens") or {}
        _entry = _tokens.get(slot_token)
        if _entry and (time.time() - _entry.get("created_at", 0)) < 600.0:
            gemini_start = _entry["slot_start_utc"]
            gemini_end = _entry["slot_end_utc"]
            _token_resolved = True
    return gemini_start, gemini_end, _token_resolved


def test_valid_token_overrides_gemini_hallucinated_iso():
    """THE PRIMARY FIX: Gemini passes hand-built naive ISO
    (2026-04-27T14:00:00 — caller's SGT wall-clock), but the authoritative
    UTC (2026-04-27T06:00:00+00:00) must be used because slot_token is
    valid."""
    deps = {}
    token = _register_slot_token(
        deps,
        "2026-04-27T06:00:00+00:00",  # authoritative
        "2026-04-27T07:00:00+00:00",
    )
    start, end, resolved = _extract_resolution_logic(
        deps,
        slot_token=token,
        gemini_start="2026-04-27T14:00:00",   # Gemini's wrong naive-SGT ISO
        gemini_end="2026-04-27T15:00:00",
    )
    assert resolved is True
    assert start == "2026-04-27T06:00:00+00:00", (
        f"token must override Gemini's hallucinated ISO; got {start!r}"
    )
    assert end == "2026-04-27T07:00:00+00:00"


def test_missing_token_falls_through_to_gemini_isos():
    """Backward compat: no slot_token → legacy path uses slot_start/slot_end
    as supplied (subject to _ensure_utc_iso coercion downstream)."""
    deps = {}
    start, end, resolved = _extract_resolution_logic(
        deps,
        slot_token="",
        gemini_start="2026-04-27T06:00:00+00:00",
        gemini_end="2026-04-27T07:00:00+00:00",
    )
    assert resolved is False
    assert start == "2026-04-27T06:00:00+00:00"
    assert end == "2026-04-27T07:00:00+00:00"


def test_unknown_token_falls_through():
    """Gemini invents a slot_token that was never registered. System must
    fall back to the legacy path (not crash)."""
    deps = {"_slot_tokens": {}}
    start, end, resolved = _extract_resolution_logic(
        deps,
        slot_token="slot_deadbeef",
        gemini_start="2026-04-27T06:00:00+00:00",
        gemini_end="2026-04-27T07:00:00+00:00",
    )
    assert resolved is False
    assert start == "2026-04-27T06:00:00+00:00"


def test_expired_token_falls_through():
    """Token older than 10 min is ignored. The caller might re-ask about a
    slot after a long pause; we re-check and re-issue rather than trust
    stale state."""
    deps = {
        "_slot_tokens": {
            "slot_expired0": {
                "slot_start_utc": "2026-04-27T06:00:00+00:00",
                "slot_end_utc": "2026-04-27T07:00:00+00:00",
                "created_at": time.time() - 700.0,  # 11 min old
            }
        }
    }
    start, end, resolved = _extract_resolution_logic(
        deps,
        slot_token="slot_expired0",
        gemini_start="2026-04-27T99:99:99",  # garbage; proves legacy path
        gemini_end="2026-04-27T99:99:99",
    )
    assert resolved is False
    assert start == "2026-04-27T99:99:99", "expired token must not override"


def test_check_availability_state_line_embeds_slot_token():
    """Every slot_available / alternatives STATE line must carry the
    slot_token so Gemini has the value to echo back."""
    # Structural check: the STATE template strings in the production code
    # must contain the `slot_token=` marker. Grep the source file directly
    # so future edits that drop the marker get caught here.
    src = open(
        "C:/Users/leheh/.Projects/livekit-agent/src/tools/check_availability.py",
        encoding="utf-8",
    ).read()
    # slot_available branch
    assert "slot_token={_token}" in src, (
        "check_availability slot_available STATE line missing slot_token marker"
    )
    # alternatives branch: token returned in ALTERNATIVES block lines
    assert "slot_token={tok}" in src, (
        "check_availability alternatives ALTERNATIVES list missing slot_token marker"
    )


def test_book_appointment_description_mentions_slot_token():
    """Tool description is what Gemini reads to decide arg shape. The
    slot_token guidance must be present and prominent."""
    src = open(
        "C:/Users/leheh/.Projects/livekit-agent/src/tools/book_appointment.py",
        encoding="utf-8",
    ).read()
    assert "CRITICAL: pass slot_token" in src, (
        "book_appointment description must prominently mention slot_token"
    )
    assert "off-by-8-hours" in src, (
        "description should cite the concrete past failure to ground the rule"
    )


# ── Hallucination-guard tests (2026-04-24 post-UAT fix) ─────────────────
#
# UAT call-_+6587528516_J8ht2s6vu3rE showed Gemini 3.1 Flash Live copy-
# pasting the literal `slot_token=slot_a1b2c3d4` example from the tool
# docstring instead of echoing the real dynamic token from the STATE
# line. Every booking attempt missed the token registry → fell through
# to the broken legacy path → caller heard "I'm still having trouble
# booking that time" on loop.
#
# Fix: (A) strip the literal example from the docstring; (C) stash a
# `_last_offered_token` on deps in check_availability's single-slot
# branch and fall back to it in book_appointment when slot_token is
# missing or unknown. Defense in depth.


def test_docstring_has_no_literal_token_example():
    """A + C regression guard: no literal `slot_<hex>` examples in the
    tool description. Gemini treats them as defaults and hallucinates."""
    src = open(
        "C:/Users/leheh/.Projects/livekit-agent/src/tools/book_appointment.py",
        encoding="utf-8",
    ).read()
    # Isolate the @function_tool description block
    desc_start = src.index('name="book_appointment"')
    desc_end = src.index("async def book_appointment", desc_start)
    desc = src[desc_start:desc_end]
    assert "slot_a1b2c3d4" not in desc, (
        "Literal example token leaked into docstring — Gemini copy-pastes it"
    )
    # Guard broader pattern: any `slot_<8-hex>` literal
    import re
    leaks = re.findall(r"slot_[0-9a-f]{8}", desc)
    assert not leaks, f"Literal hex-token examples in docstring: {leaks}"


def _extract_resolution_logic_v2(deps: dict, slot_token: str,
                                 gemini_start: str, gemini_end: str) -> tuple[str, str, bool, str]:
    """Updated shadow of book_appointment.py resolution block (2026-04-24)
    that includes the `_last_offered_token` fallback. Returns
    (slot_start, slot_end, token_resolved, effective_token)."""
    _tokens = deps.get("_slot_tokens") or {}
    if slot_token and slot_token not in _tokens:
        _last_offered = deps.get("_last_offered_token")
        if _last_offered and _last_offered in _tokens:
            slot_token = _last_offered
    elif not slot_token:
        _last_offered = deps.get("_last_offered_token")
        if _last_offered and _last_offered in _tokens:
            slot_token = _last_offered
    _token_resolved = False
    if slot_token:
        _entry = _tokens.get(slot_token)
        if _entry and (time.time() - _entry.get("created_at", 0)) < 600.0:
            gemini_start = _entry["slot_start_utc"]
            gemini_end = _entry["slot_end_utc"]
            _token_resolved = True
    return gemini_start, gemini_end, _token_resolved, slot_token


def test_no_token_falls_back_to_last_offered():
    """C: Gemini omits slot_token entirely; deps has _last_offered_token
    from the prior check_availability call. Must recover."""
    deps = {}
    token = _register_slot_token(
        deps, "2026-04-27T06:00:00+00:00", "2026-04-27T07:00:00+00:00",
    )
    deps["_last_offered_token"] = token
    start, end, resolved, effective = _extract_resolution_logic_v2(
        deps, slot_token="",
        gemini_start="", gemini_end="",
    )
    assert resolved is True
    assert effective == token
    assert start == "2026-04-27T06:00:00+00:00"


def test_hallucinated_token_falls_back_to_last_offered():
    """C: Gemini passes the literal docstring example `slot_a1b2c3d4`
    (not in registry). Must recover via _last_offered_token."""
    deps = {}
    real = _register_slot_token(
        deps, "2026-04-27T06:00:00+00:00", "2026-04-27T07:00:00+00:00",
    )
    deps["_last_offered_token"] = real
    start, end, resolved, effective = _extract_resolution_logic_v2(
        deps, slot_token="slot_a1b2c3d4",  # hallucinated example
        gemini_start="2026-04-27T14:00:00",   # naive SGT digits, wrong
        gemini_end="2026-04-27T15:00:00",
    )
    assert resolved is True
    assert effective == real, "must recover to real registered token"
    assert start == "2026-04-27T06:00:00+00:00"


def test_no_last_offered_and_no_token_does_not_recover():
    """Guardrail: if the alternatives branch cleared _last_offered_token
    and Gemini also sent no token, we do NOT silently invent one."""
    deps = {"_slot_tokens": {}}  # empty registry, no _last_offered_token
    start, end, resolved, effective = _extract_resolution_logic_v2(
        deps, slot_token="",
        gemini_start="2026-04-27T06:00:00+00:00",
        gemini_end="2026-04-27T07:00:00+00:00",
    )
    assert resolved is False
    assert effective == ""


def test_alternatives_branch_clears_last_offered():
    """Structural check: check_availability's alternatives branch must
    pop _last_offered_token so a caller's ambiguous pick doesn't
    silently bind to a stale single-slot token."""
    src = open(
        "C:/Users/leheh/.Projects/livekit-agent/src/tools/check_availability.py",
        encoding="utf-8",
    ).read()
    assert 'deps.pop("_last_offered_token"' in src, (
        "alternatives branch must clear _last_offered_token"
    )


def test_single_slot_branch_sets_last_offered():
    """Structural check: check_availability's single-slot branch must
    stash _last_offered_token for book_appointment's fallback."""
    src = open(
        "C:/Users/leheh/.Projects/livekit-agent/src/tools/check_availability.py",
        encoding="utf-8",
    ).read()
    assert 'deps["_last_offered_token"] = _token' in src, (
        "single-slot branch must set _last_offered_token"
    )


def test_successful_booking_clears_last_offered():
    """Structural check: on successful booking, _last_offered_token is
    cleared so a subsequent booking in the same call cannot silently
    reuse the just-booked slot."""
    src = open(
        "C:/Users/leheh/.Projects/livekit-agent/src/tools/book_appointment.py",
        encoding="utf-8",
    ).read()
    assert 'deps.pop("_last_offered_token"' in src, (
        "successful booking path must clear _last_offered_token"
    )
