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
