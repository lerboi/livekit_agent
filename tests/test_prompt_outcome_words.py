"""Phase 60.3 Plan 09 — _build_outcome_words_section locale parity.

Stream B patch addresses 60.3-PROMPT-AUDIT.md §_build_outcome_words_section
(dimensions D1, D4, D7). This is the single highest-stakes anti-hallucination
surface in the prompt: a caller who hangs up believing they have a confirmed
appointment when nothing is in the system is the worst possible failure. A
Spanish caller fabricated "3pm está disponible" without a check_availability
call is identically catastrophic — hence the D7 locale-parity gap.

Invariants asserted here:
1. EN output contains the "OUTCOME WORDS — CRITICAL RULE:" header.
2. ES output contains the "PALABRAS DE RESULTADO — REGLA CRÍTICA:" header.
3. EN enumerates reserved words and tool names verbatim: "available",
   "not available", "confirmed", "booked", "check_availability",
   "book_appointment".
4. ES enumerates reserved words in Spanish ("disponible", "no disponible",
   "confirmado", "reservado") AND preserves tool names un-translated
   ("check_availability", "book_appointment" are code identifiers, not prose).
5. EN failure-mode example uses "3pm" and "WRONG" framing with tool-free
   fabrication pattern.
6. ES failure-mode example uses "3pm" / "3 pm" / "15:00" (Spanish markets
   vary on clock convention) and "INCORRECTO" framing.
7. Both locales carry "worst failure mode" framing
   (EN "worst failure mode"; ES "peor modo de falla" OR "peor escenario").
8. Both locales declare silence acceptable during tool execution
   (EN "silence" + "acceptable"; ES "silencio" + "aceptable").
9. EN and ES strings are distinct (parity guard against copy-paste error).
"""
from __future__ import annotations

from src.prompt import _build_outcome_words_section


def test_en_critical_rule_heading():
    section = _build_outcome_words_section("en")
    assert isinstance(section, str)
    assert "OUTCOME WORDS — CRITICAL RULE:" in section


def test_es_critical_rule_heading():
    # 2026-06-11 single-prompt collapse: locale="es" returns the same English
    # body — invariant (outcome-words CRITICAL RULE present for es-locale
    # calls) maps to the EN header.
    section = _build_outcome_words_section("es")
    assert isinstance(section, str)
    assert "OUTCOME WORDS — CRITICAL RULE:" in section


def test_en_reserved_words_enumerated():
    # 2026-06-10: tool pin updated from the retired monolithic
    # check_availability to check_slot (the split tool that licenses the
    # 'available' reserved word). Same invariant — reserved words map to
    # real, untranslated tool identifiers.
    section = _build_outcome_words_section("en")
    for word in (
        "available",
        "not available",
        "confirmed",
        "booked",
        "check_slot",
        "book_appointment",
    ):
        assert word in section, f"EN missing reserved word/tool: {word!r}"


def test_es_reserved_words_enumerated():
    # Tool names (check_slot, book_appointment) are code identifiers wired
    # to src/tools/ — they MUST NOT be translated. Reserved Spanish words
    # (disponible/no disponible/confirmado/reservado) MUST appear.
    # (2026-06-10: check_availability → check_slot.)
    # 2026-06-11 collapse: the Spanish forms now live in the unified EN
    # section's "in any language, including Spanish" clause — same invariant,
    # no ES branch needed.
    section = _build_outcome_words_section("es")
    for word in (
        "disponible",
        "no disponible",
        "confirmado",
        "reservado",
        "check_slot",
        "book_appointment",
    ):
        assert word in section, f"ES missing reserved word/tool: {word!r}"


def test_en_failure_mode_3pm_example():
    section = _build_outcome_words_section("en")
    assert "3pm" in section
    assert "WRONG" in section
    # Characterization of tool-free fabrication — the example must show the
    # model claiming availability without a tool call in the same turn.
    # (2026-06-10: check_availability → check_slot, the real tool name.)
    assert "check_slot" in section


def test_es_failure_mode_3pm_example():
    # 2026-06-11 collapse: the failure-mode teaching invariant maps to the EN
    # example (3pm + WRONG) for es-locale calls too.
    section = _build_outcome_words_section("es")
    assert "3pm" in section
    assert "WRONG" in section


def test_both_locales_worst_failure_mode_framing():
    # 2026-06-11 collapse: worst-failure framing unchanged; EN text in both.
    for locale in ("en", "es"):
        assert "worst failure mode" in _build_outcome_words_section(locale).lower()


def test_both_locales_silence_acceptable():
    # 2026-06-11 collapse: silence-during-tool license unchanged; EN in both.
    for locale in ("en", "es"):
        lowered = _build_outcome_words_section(locale).lower()
        assert "silence" in lowered and "acceptable" in lowered


def test_en_es_identical():
    # 2026-06-11 collapse: the old distinctness guard inverts — this section
    # must NOT fork on locale. The Spanish reserved-word forms now live in
    # the bilingual any-language clause (asserted by
    # test_es_reserved_words_enumerated above).
    en = _build_outcome_words_section("en")
    es = _build_outcome_words_section("es")
    assert en == es
    # And the bilingual clause must stay framed as any-language.
    assert "in any language, including Spanish" in en
