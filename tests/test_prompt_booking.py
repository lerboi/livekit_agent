"""Phase 60.3 Plan 11 — _build_booking_section invariant lock + D7 parity.

Stream B patch addresses 60.3-PROMPT-AUDIT.md §_build_booking_section
(dimensions D1 anti-hallucination CRITICAL + D7 locale parity — was ~,
now ✓).

Prior state (per R-B1): the section carried an `if locale == "es":` branch
covering ONLY the BEFORE BOOKING — READBACK block (~15% of the section).
The outer BOOKING + SCHEDULING + AVAILABILITY RULES + HANDLING THE RESULT
+ AFTER BOOKING blocks were English-only — roughly 85% of the section.
Spanish callers received a Spanish readback wrapped in English booking
rules — a broken locale experience at the prompt's single most
anti-hallucination-critical checkpoint (commit to book_appointment).

This plan closes the gap by:

- Restructuring the body into composable local variables per Plan 10's
  invariant-lock template.
- Adding a full ES outer frame (BOOKING/SCHEDULING/AVAILABILITY/HANDLING
  THE RESULT/AFTER BOOKING) mirroring the EN structure.
- Preserving the pre-existing ES readback block verbatim (R-B1 — the
  readback prose was already correct; don't rewrite).
- Asserting both locales carry: the two-step availability contract
  (check_availability BEFORE book_appointment), the readback invariant
  (time + address), and the anti-fabrication rule (never say "confirmed"
  before book_appointment returns success).
- Asserting tool names (check_availability, book_appointment) are NEVER
  translated — they're Python identifiers wired to src/tools/.
- Asserting postal_label parametrization (SG "postal code" / US "zip
  code") flows through both locales' address-readback prose.
- Asserting onboarding_complete=False path emits a simplified lead-capture
  prose in both locales.
- Asserting EN/ES length stays within 30% drift guard (catches one-sided
  future compressions).
"""
from __future__ import annotations

import pytest

from src.prompt import _build_booking_section


# ---------------------------------------------------------------------------
# Baseline nonempty + parity
# ---------------------------------------------------------------------------


def test_en_nonempty():
    section = _build_booking_section("Voco", True, "postal code", "en")
    assert isinstance(section, str)
    assert len(section) > 500


def test_es_nonempty():
    # Prior to Plan 11, ES was effectively only the readback block
    # (~300 chars). Post-Plan-11, ES must carry the full protocol.
    section = _build_booking_section("Voco", True, "postal code", "es")
    assert isinstance(section, str)
    assert len(section) > 500


def test_en_es_length_within_1pt5x():
    en = _build_booking_section("Voco", True, "postal code", "en")
    es = _build_booking_section("Voco", True, "postal code", "es")
    delta = abs(len(en) - len(es)) / max(len(en), len(es))
    assert delta < 0.30, f"EN/ES length drift exceeds 30%: delta={delta:.3f}"


# ---------------------------------------------------------------------------
# D1 anti-hallucination: two-step availability contract (check_availability
# BEFORE book_appointment). Tool names must NOT be translated.
# ---------------------------------------------------------------------------


def test_en_availability_contract():
    section = _build_booking_section("Voco", True, "postal code", "en")
    lowered = section.lower()
    assert "check_availability" in lowered
    assert "before" in lowered
    assert "book" in lowered


def test_es_availability_contract():
    section = _build_booking_section("Voco", True, "postal code", "es")
    lowered = section.lower()
    # Tool names NOT translated.
    assert "check_availability" in lowered
    # Temporal contract word in Spanish.
    assert "antes" in lowered
    # Booking concept present — either verb or tool name.
    assert ("reservar" in lowered) or ("book_appointment" in lowered)


# ---------------------------------------------------------------------------
# D1 anti-hallucination: readback invariant — caller hears time + address
# back before book_appointment commits.
# ---------------------------------------------------------------------------


def test_en_readback_invariant():
    section = _build_booking_section("Voco", True, "postal code", "en")
    lowered = section.lower()
    # Readback cue present.
    assert ("read back" in lowered) or ("confirm" in lowered)
    # Address concept in scope.
    assert "address" in lowered


def test_es_readback_invariant():
    section = _build_booking_section("Voco", True, "postal code", "es")
    lowered = section.lower()
    # Readback/confirmation cue present.
    assert "confirmar" in lowered or "confirme" in lowered or "confirma" in lowered
    # Address concept in scope.
    assert "dirección" in lowered


# ---------------------------------------------------------------------------
# postal_label parametrization — both locales must substitute the passed
# label into their address-readback prose.
# ---------------------------------------------------------------------------


def test_postal_label_propagates_en_postal_code():
    section = _build_booking_section("Voco", True, "postal code", "en")
    assert "postal code" in section


def test_postal_label_propagates_en_zip_code():
    section = _build_booking_section("Voco", True, "zip code", "en")
    assert "zip code" in section


def test_postal_label_propagates_es_postal_code():
    # ES must respect the caller's country regardless of locale —
    # postal_label is a market signal (SG vs US), not a language signal.
    section = _build_booking_section("Voco", True, "postal code", "es")
    assert "postal code" in section


def test_postal_label_propagates_es_zip_code():
    section = _build_booking_section("Voco", True, "zip code", "es")
    assert "zip code" in section


# ---------------------------------------------------------------------------
# onboarding_complete=False path — both locales emit simplified lead-
# capture prose (no booking protocol); onboarding_complete=True path
# emits the full 3-step protocol.
# ---------------------------------------------------------------------------


def test_en_onboarding_incomplete_simplified():
    section = _build_booking_section("Voco", False, "postal code", "en")
    assert isinstance(section, str)
    assert section
    lowered = section.lower()
    # Simplified path — mentions business name and lead-capture flow.
    assert "voco" in lowered
    # Full 3-step protocol NOT present.
    assert "check_availability" not in lowered


def test_es_onboarding_incomplete_simplified():
    section = _build_booking_section("Voco", False, "postal code", "es")
    assert isinstance(section, str)
    assert section
    lowered = section.lower()
    assert "voco" in lowered
    # Full 3-step protocol NOT present in simplified path.
    assert "check_availability" not in lowered


def test_both_locales_onboarding_gated_full_protocol():
    en_full = _build_booking_section("Voco", True, "postal code", "en")
    es_full = _build_booking_section("Voco", True, "postal code", "es")
    # Full protocol path contains the two-step contract.
    assert "check_availability" in en_full.lower()
    assert "check_availability" in es_full.lower()


# ---------------------------------------------------------------------------
# Parity guard — EN and ES must be distinct (no copy-paste regression) and
# both must be non-empty at the full-protocol path.
# ---------------------------------------------------------------------------


def test_en_es_distinct_and_nonempty_full_protocol():
    en = _build_booking_section("Voco", True, "postal code", "en")
    es = _build_booking_section("Voco", True, "postal code", "es")
    assert en
    assert es
    assert en != es


# ---------------------------------------------------------------------------
# Anti-fabrication rule — model must NOT say "confirmed"/"booked" before
# book_appointment returns success. Present in both locales.
# ---------------------------------------------------------------------------


def test_en_anti_fabrication_rule():
    section = _build_booking_section("Voco", True, "postal code", "en")
    lowered = section.lower()
    # "confirmed" or "booked" as reserved words gated on book_appointment
    # success — phrase flexibility allowed.
    assert "book_appointment" in lowered
    assert ("confirmed" in lowered) or ("booked" in lowered)


def test_es_anti_fabrication_rule():
    section = _build_booking_section("Voco", True, "postal code", "es")
    lowered = section.lower()
    assert "book_appointment" in lowered
    assert ("confirmado" in lowered) or ("reservado" in lowered)


# ──────────────────────────────────────────────────────────────────────────
# Phase 64 D-03d — NO DOUBLE-BOOKING one-liner for pipeline architecture.
#
# Pipeline tool lifecycle is framework-tracked (AgentActivity runs
# @function_tool coroutines locally; results inserted into chat_ctx as
# FunctionCallOutput). The 'server cancelled tool calls' race that motivated
# the original ~8-line NO DOUBLE-BOOKING block on the Realtime path does
# not exist on pipeline. The block collapses to a concise one-liner.
#
# What MUST be preserved:
# - check_availability BEFORE book_appointment two-step contract (all
#   pre-existing Phase 60.3-11 tests above assert this)
# - book_appointment tool name in English (not translated)
# - "once per slot" semantic (whatever phrasing Plan 03 chooses)
# - readback + anti-fabrication rules
#
# What MUST be removed:
# - [TOKEN_FROM_LAST_TOOL_RESULT] placeholder scaffolding (EN)
# - REPLACE_WITH_ACTUAL_TOKEN placeholder scaffolding (EN)
# - The multi-line "caller noise does not mean booking failed" sub-bullet
#
# Tests below are RED in Plan 01 — they fail against the current 8-line
# block. Plan 03 turns them GREEN by simplifying _build_booking_section.
# Pre-existing 17 tests must remain GREEN.
# ──────────────────────────────────────────────────────────────────────────


def _extract_no_double_booking_block(section: str, header: str = "NO DOUBLE-BOOKING") -> str:
    """Extract the NO DOUBLE-BOOKING sub-block for size assertions.

    Returns empty string if the block was folded into surrounding prose
    (acceptable D-03d outcome per planner discretion).
    """
    lines = section.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if header in l)
    except StopIteration:
        return ""  # block dissolved into surrounding prose — acceptable
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i].strip()
        if line and line.isupper() and line.endswith(":"):
            end = i
            break
    return "\n".join(lines[start:end])


def test_en_no_double_booking_is_one_liner():
    """D-03d: EN NO DOUBLE-BOOKING block collapses to a one-liner.

    Measured as a character budget so the test is insensitive to whether
    the block is one wrapped line or two short lines — what matters is
    the Realtime-race scaffolding prose is gone. Current pre-D-03d block
    is ~760 chars. Target post-D-03d: ≤ 300 chars (header + concise body).
    """
    section = _build_booking_section("Voco", True, "zip code", "en")
    block = _extract_no_double_booking_block(section, "NO DOUBLE-BOOKING")
    # If block dissolved entirely into surrounding prose, planner folded it —
    # that too satisfies D-03d. Only fail if the block exists AND exceeds budget.
    if block:
        char_count = len(block)
        assert char_count <= 300, (
            f"Phase 64 D-03d: NO DOUBLE-BOOKING block should be a one-liner "
            f"(≤ 300 chars); got {char_count} chars. Realtime-race scaffolding "
            f"('caller noise does not mean', placeholder tokens, slot_token "
            f"recovery branch) must be removed.\nBlock:\n{block}"
        )


def test_en_no_double_booking_drops_realtime_placeholders():
    """D-03d: Realtime-race placeholder scaffolding must be removed (EN)."""
    section = _build_booking_section("Voco", True, "zip code", "en")
    assert "[TOKEN_FROM_LAST_TOOL_RESULT]" not in section, (
        "Phase 64 D-03d: [TOKEN_FROM_LAST_TOOL_RESULT] placeholder was "
        "Realtime-race scaffolding — pipeline tool lifecycle removes this need"
    )
    assert "REPLACE_WITH_ACTUAL_TOKEN" not in section, (
        "Phase 64 D-03d: REPLACE_WITH_ACTUAL_TOKEN placeholder was "
        "Realtime-race scaffolding — pipeline tool lifecycle removes this need"
    )


def test_es_no_double_booking_drops_realtime_placeholders():
    """D-03d: Realtime-race placeholder scaffolding must be removed (ES).

    Note: the current ES branch never contained these English placeholder
    strings (they were EN-only), so this test passes today. It's a guard
    against Plan 03 accidentally introducing them while re-authoring ES.
    """
    section = _build_booking_section("Voco", True, "código postal", "es")
    assert "[TOKEN_FROM_LAST_TOOL_RESULT]" not in section
    assert "REPLACE_WITH_ACTUAL_TOKEN" not in section


def test_no_double_booking_preserves_once_per_slot_invariant_en():
    """D-03d: the simplified one-liner still communicates once-per-slot."""
    section = _build_booking_section("Voco", True, "zip code", "en")
    # book_appointment tool name preserved English (Phase 60.3-06 convention)
    assert "book_appointment" in section
    lowered = section.lower()
    # One of these tokens must carry the once-per-slot semantic
    assert ("once" in lowered) or ("only" in lowered) or ("do not call" in lowered), (
        "Phase 64 D-03d: EN one-liner must still communicate once-per-slot semantic"
    )


def test_no_double_booking_preserves_once_per_slot_invariant_es():
    """D-03d: the simplified one-liner still communicates once-per-slot in ES.

    Parity with EN: current ES branch lacks a NO DOUBLE-BOOKING block
    entirely (pre-D-03d state). Plan 03 must ADD a concise ES one-liner
    mirroring EN. The assertion checks that a once-per-slot phrase appears
    within a 200-char window around the first `book_appointment` mention —
    which rules out accidental matches elsewhere in the section (e.g.
    'una vez que tenga el nombre' from SCHEDULING block).
    """
    section = _build_booking_section("Voco", True, "código postal", "es")
    # book_appointment tool name preserved English (Phase 60.3-06 convention)
    assert "book_appointment" in section
    lowered = section.lower()
    # Find book_appointment mentions and check proximity to once-per-slot phrases
    phrases = ["una sola vez", "solamente una vez", "solo una vez", "no llame a book_appointment"]
    idx = 0
    found_proximate = False
    while True:
        idx = lowered.find("book_appointment", idx)
        if idx == -1:
            break
        window = lowered[max(0, idx - 200): idx + 200]
        if any(p in window for p in phrases):
            found_proximate = True
            break
        idx += len("book_appointment")
    assert found_proximate, (
        f"Phase 64 D-03d: ES must carry a once-per-slot one-liner NEAR "
        f"book_appointment (within 200 chars). Phrases looked for: {phrases}. "
        f"USTED register required."
    )


def test_check_availability_before_book_appointment_preserved_en():
    """D-03d regression guard: two-step contract from 60.3-11 preserved (EN)."""
    section = _build_booking_section("Voco", True, "zip code", "en")
    # Core two-step invariant from Phase 60.3-11 MUST NOT regress under D-03d
    assert "check_availability" in section
    assert "book_appointment" in section


def test_check_availability_before_book_appointment_preserved_es():
    """D-03d regression guard: two-step contract from 60.3-11 preserved (ES)."""
    section = _build_booking_section("Voco", True, "código postal", "es")
    assert "check_availability" in section
    assert "book_appointment" in section


def test_readback_rules_preserved_both_locales_after_d03d():
    """D-03d regression guard: BEFORE BOOKING — READBACK block still present."""
    en = _build_booking_section("Voco", True, "zip code", "en").lower()
    es = _build_booking_section("Voco", True, "código postal", "es").lower()
    # EN: "read back" present
    assert "read back" in en, (
        "Phase 64 D-03d: EN BEFORE BOOKING — READBACK block must remain"
    )
    # ES: "lea" (USTED imperative) or "lectura" (block title) present
    assert ("lea" in es) or ("lectura" in es), (
        "Phase 64 D-03d: ES BEFORE BOOKING — READBACK block must remain"
    )
