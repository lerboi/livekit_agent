"""Best-practices optimization (2026-06) — FINAL non-negotiables recap.

The recap restates the must-win invariants at the recency position (last in the
assembled prompt). Rationale: GPT-4.1 follows the LATER of two conflicting
instructions, and long-context models attend most strongly to the start/end
("lost in the middle"). These tests lock: header presence per locale, the four
recap items, EN/ES distinctness, and that the recap is the LAST thing in the
assembled prompt (so nothing dilutes the recency placement).
"""
from __future__ import annotations

from src.prompt import _build_final_nonnegotiables_section, build_system_prompt


def test_en_header_and_items():
    s = _build_final_nonnegotiables_section("en")
    assert "FINAL — NON-NEGOTIABLES" in s
    lowered = s.lower()
    assert "available" in lowered and "confirmed" in lowered  # anti-fabrication
    assert "book the same slot again" in lowered              # no double-booking
    assert "end_call" in s                                    # clean goodbye
    assert "brief description" in lowered                     # brief problem


def test_es_header_and_items():
    s = _build_final_nonnegotiables_section("es")
    assert "FINAL — INNEGOCIABLES" in s
    lowered = s.lower()
    assert "disponible" in lowered and "confirmado" in lowered
    assert "end_call" in s
    assert "descripción breve" in lowered


def test_en_es_distinct():
    assert _build_final_nonnegotiables_section("en") != _build_final_nonnegotiables_section("es")


def test_recap_is_last_in_assembled_en():
    assembled = build_system_prompt(locale="en", business_name="Voco", onboarding_complete=True)
    # The recap must be the final section — nothing follows it (recency placement).
    assert assembled.rstrip().endswith("Don't interrogate the caller about the situation.")
    # And it appears after the booking section (a mid-prompt anchor).
    assert assembled.index("FINAL — NON-NEGOTIABLES") > assembled.index("BOOKING:")


def test_recap_is_last_in_assembled_es():
    assembled = build_system_prompt(locale="es", business_name="Voco", onboarding_complete=True)
    assert assembled.rstrip().endswith("No interrogue al llamante sobre la situación.")
