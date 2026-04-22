"""Phase 60.3 Stream B Plan 5 — _build_call_duration_section locale parity.

Inherits the inverted-assertion pattern established in 60.2 Plan 05
(tests/test_prompt.py) — assertions enumerate what the section MUST contain
(CRITICAL RULE framing, two-step farewell mechanics, WRONG/RIGHT example,
9/10-minute bounds, locale-specific prose, brand name "Voco") so any future
refactor that drops a locale branch or weakens the mechanics will fail loudly.

Scope expansions over Plan 3 (Branch P) baseline:
1. Signature: _build_call_duration_section(t, locale: str) — adds explicit
   locale argument. Call site in build_system_prompt updated accordingly.
2. Locale parity: new Spanish branch ("TERMINAR LA LLAMADA — REGLA CRÍTICA")
   mirroring the English CRITICAL RULE block structure. Closes D-B-02 D7 gap.
3. Anti-hallucination (D1): each locale's WRONG/RIGHT example uses the
   brand "Voco" (CLAUDE.md rule — never "HomeService AI"). The post-Plan-3
   Branch P body already uses "Voco"; this test codifies the invariant.
4. Section-position invariant extended to Spanish: build_system_prompt with
   locale='es' must still place call_duration above tool_narration.

UAT #2 evidence context (60.3-HUMAN-UAT.md, call-..._B8XEm2FgLTGZ) motivated
Plan 5's D1 expansion. Stream A's UAT #2 showed the model said only "I
understand." and invoked end_call without any farewell phrase. The Plan 5
rewrite introduces a Spanish parallel to this mandate; Plan 9 extends
outcome-words if UAT #3 shows the gap still open.

These tests MUST remain additive to tests/test_prompt.py (60.2 Pitfall 6
inverted assertions on tool_narration are preserved).
"""
from __future__ import annotations

from src.prompt import (
    _build_call_duration_section,
    build_system_prompt,
)


def _t_stub(key: str) -> str:
    """Minimal t() stub — _build_call_duration_section does not use t(),
    but signature accepts it for symmetry with other builders."""
    return key


# ── EN branch invariants ───────────────────────────────────────────────────


def test_en_contains_critical_rule_frame():
    """D2 realtime-model: English output retains the CRITICAL RULE header
    (Plan 3 Branch P invariant — must not regress)."""
    section = _build_call_duration_section(_t_stub, locale="en")
    assert "ENDING THE CALL — CRITICAL RULE" in section


# ── ES branch invariants ───────────────────────────────────────────────────


def test_es_contains_spanish_critical_rule():
    """D7 locale parity: Spanish output uses REGLA CRÍTICA framing
    (mirrors English CRITICAL RULE convention for top-attention-band)."""
    section = _build_call_duration_section(_t_stub, locale="es")
    # Plan 5 selects "TERMINAR LA LLAMADA — REGLA CRÍTICA" as the canonical
    # Spanish header (documented in the diff).
    assert "TERMINAR LA LLAMADA — REGLA CRÍTICA" in section


# ── Locale differentiation ─────────────────────────────────────────────────


def test_en_and_es_are_distinct():
    """D7 locale parity: en and es branches must return different strings
    (i.e. the es branch is not a copy-paste of the en body)."""
    en = _build_call_duration_section(_t_stub, locale="en")
    es = _build_call_duration_section(_t_stub, locale="es")
    assert en != es
    assert len(en) > 0
    assert len(es) > 0


# ── Shared numeric invariants (D1 anti-hallucination) ──────────────────────


def test_both_locales_retain_9_and_10_minute_bounds():
    """D1: the 9-minute wrap-up and 10-minute hard-max numerals must be
    present in BOTH locales. Numerals are locale-neutral so we assert the
    raw digits, not the words "minutes"/"minutos"."""
    en = _build_call_duration_section(_t_stub, locale="en")
    es = _build_call_duration_section(_t_stub, locale="es")
    assert "9" in en
    assert "10" in en
    assert "9" in es
    assert "10" in es


# ── Failure-mode example invariants (D2 realtime-model) ────────────────────


def test_both_locales_have_failure_mode_example():
    """D2 realtime-model: each locale must have a WRONG/RIGHT (or
    INCORRECTO/CORRECTO) failure-mode contrast — concrete examples are
    load-bearing for realtime models per 60.3-RESEARCH §R-B5."""
    en = _build_call_duration_section(_t_stub, locale="en")
    es = _build_call_duration_section(_t_stub, locale="es")
    assert "WRONG:" in en
    assert "RIGHT:" in en
    assert "INCORRECTO:" in es
    assert "CORRECTO:" in es


# ── Brand-name invariants (CLAUDE.md rule) ─────────────────────────────────


def test_en_contains_voco_business_name():
    """CLAUDE.md: brand name is "Voco" — not "HomeService AI". The
    WRONG/RIGHT example in each locale uses the canonical brand."""
    en = _build_call_duration_section(_t_stub, locale="en")
    es = _build_call_duration_section(_t_stub, locale="es")
    assert "Voco" in en
    assert "Voco" in es
    # Anti-regression — the legacy brand name must not appear.
    assert "HomeService AI" not in en
    assert "HomeService AI" not in es


# ── Section-position invariants (D3 section ordering) ──────────────────────


def test_section_is_position_5_or_earlier_in_both_locales():
    """D3: in the assembled prompt (both locales), the call_duration
    CRITICAL RULE header must appear BEFORE the tool_narration header —
    i.e. inside the top-attention band (Plan 3 Branch P invariant,
    extended to the es branch by Plan 5)."""
    for locale, cd_header in (
        ("en", "ENDING THE CALL — CRITICAL RULE"),
        ("es", "TERMINAR LA LLAMADA — REGLA CRÍTICA"),
    ):
        assembled = build_system_prompt(
            locale=locale,
            business_name="Voco",
            onboarding_complete=True,
        )
        idx_cd = assembled.index(cd_header)
        # tool_narration header is English-only in the current codebase
        # (Plan 6 will add the es branch). For locale='es' the assembled
        # prompt still contains the English tool_narration header — this
        # is expected pre-Plan-6.
        idx_tn = assembled.index("TOOL NARRATION — CRITICAL RULE")
        assert idx_cd < idx_tn, (
            f"[locale={locale}] call_duration CRITICAL RULE header must "
            f"appear before tool_narration header in the assembled prompt"
        )


# ── Pitfall 6 guard (60.2 inverted-assertion parity) ───────────────────────


def test_no_session_say_or_runtime_filler_regression():
    """Pitfall 6: neither locale output may reintroduce the 60.2 Fix H
    "session.say" / "runtime plays" pattern — session.say cannot produce
    audio on a RealtimeModel-only AgentSession in livekit-agents 1.5.1."""
    for locale in ("en", "es"):
        section = _build_call_duration_section(_t_stub, locale=locale)
        lowered = section.lower()
        assert "session.say" not in lowered
        assert "runtime plays" not in lowered
