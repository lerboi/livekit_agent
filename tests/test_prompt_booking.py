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
    # 2026-06-10: pins updated from the retired monolithic `check_availability`
    # to the real availability tool names (prod-readiness split:
    # check_slot / check_day / next_available_days). Same invariant — the
    # two-step availability contract names real, untranslated tool ids.
    section = _build_booking_section("Voco", True, "postal code", "en")
    lowered = section.lower()
    assert "check_slot" in lowered
    assert "check_day" in lowered
    assert "next_available_days" in lowered
    assert "before" in lowered
    assert "book" in lowered


def test_es_availability_contract():
    # 2026-06-11 single-prompt collapse: locale="es" returns the same English
    # body — the two-step availability contract pins map to the EN words.
    section = _build_booking_section("Voco", True, "postal code", "es")
    lowered = section.lower()
    # Tool names NOT translated. (2026-06-10: check_availability → split tools.)
    assert "check_slot" in lowered
    assert "check_day" in lowered
    assert "next_available_days" in lowered
    # Temporal contract word.
    assert "before" in lowered
    # Booking concept present.
    assert "book_appointment" in lowered


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
    # 2026-06-11 collapse: readback invariant unchanged; es-locale output is
    # the EN body — pins map to the EN words.
    section = _build_booking_section("Voco", True, "postal code", "es")
    lowered = section.lower()
    # Readback/confirmation cue present.
    assert ("read back" in lowered) or ("confirm" in lowered)
    # Address concept in scope.
    assert "address" in lowered


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
    # Full 3-step protocol NOT present. (2026-06-10: pin moved from the
    # retired check_availability to the real availability tool names.)
    assert "check_slot" not in lowered
    assert "check_day" not in lowered


def test_es_onboarding_incomplete_simplified():
    section = _build_booking_section("Voco", False, "postal code", "es")
    assert isinstance(section, str)
    assert section
    lowered = section.lower()
    assert "voco" in lowered
    # Full 3-step protocol NOT present in simplified path.
    assert "check_slot" not in lowered
    assert "check_day" not in lowered


def test_both_locales_onboarding_gated_full_protocol():
    en_full = _build_booking_section("Voco", True, "postal code", "en")
    es_full = _build_booking_section("Voco", True, "postal code", "es")
    # Full protocol path contains the two-step contract. (2026-06-10: pin
    # moved from the retired check_availability to check_slot.)
    assert "check_slot" in en_full.lower()
    assert "check_slot" in es_full.lower()


# ---------------------------------------------------------------------------
# Parity guard — EN and ES must be distinct (no copy-paste regression) and
# both must be non-empty at the full-protocol path.
# ---------------------------------------------------------------------------


def test_en_es_identical_and_nonempty_full_protocol():
    # 2026-06-11 collapse: the old distinctness guard inverts — this section
    # must NOT fork on locale anymore (es gains EN's NO DOUBLE-BOOKING block).
    en = _build_booking_section("Voco", True, "postal code", "en")
    es = _build_booking_section("Voco", True, "postal code", "es")
    assert en
    assert en == es


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
    # 2026-06-11 collapse: anti-fabrication invariant unchanged; es-locale
    # output is the EN body — Spanish reserved words now live in OUTCOME
    # WORDS' any-language clause (see test_prompt_outcome_words.py).
    section = _build_booking_section("Voco", True, "postal code", "es")
    lowered = section.lower()
    assert "book_appointment" in lowered
    assert ("confirmed" in lowered) or ("booked" in lowered)
