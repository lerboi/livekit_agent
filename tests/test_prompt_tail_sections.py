"""
Phase 60.3 Plan 12: Tail-sections batch locale parity tests.

Covers 8 sections targeted by CONTEXT D-B-04 batch:
  identity, working_hours, greeting, language, customer_account,
  intake_questions, decline_handling, transfer.

(_build_repeat_caller_section is excluded — returns "" by design; no-op.)

Primary dimension: D7 locale parity. Test pattern: inverted substring
assertions in both EN and ES, keeping tool names / dict keys / data
identifiers untranslated per prior plan convention.
"""

import json
from pathlib import Path

import pytest

from src.prompt import (
    _build_identity_section,
    _build_working_hours_section,
    _build_greeting_section,
    _build_language_section,
    _build_customer_account_section,
    _build_intake_questions_section,
    _build_decline_handling_section,
    _build_transfer_section,
    build_system_prompt,
)

_messages_dir = Path(__file__).parent.parent / "src" / "messages"
with open(_messages_dir / "en.json", "r", encoding="utf-8") as f:
    _en = json.load(f)
with open(_messages_dir / "es.json", "r", encoding="utf-8") as f:
    _es = json.load(f)


def _make_t(locale: str):
    msgs = _en if locale == "en" else _es

    def t(key: str) -> str:
        parts = key.split(".")
        val = msgs
        for part in parts:
            if isinstance(val, dict):
                val = val.get(part)
            else:
                return key
        return val if val is not None else key

    return t


# --- identity -----------------------------------------------------------------


def test_identity_en_contains_business_name():
    out = _build_identity_section("Voco", "measured and formal", "en")
    assert "Voco" in out
    assert "AI phone receptionist" in out


def test_identity_es_contains_business_name():
    # 2026-06-11 single-prompt collapse: locale="es" returns the same EN
    # body — the invariant (identity renders for es-locale calls with the
    # business name) maps to the EN pins.
    out = _build_identity_section("Voco", "measured and formal", "es")
    assert "Voco" in out
    assert "AI phone receptionist" in out


# --- working_hours ------------------------------------------------------------


_SAMPLE_HOURS = {
    "monday": {"enabled": True, "open": "09:00", "close": "17:00"},
    "tuesday": {"enabled": True, "open": "09:00", "close": "17:00"},
    "wednesday": {"enabled": True, "open": "09:00", "close": "17:00"},
    "thursday": {"enabled": True, "open": "09:00", "close": "17:00"},
    "friday": {"enabled": True, "open": "09:00", "close": "17:00"},
    "saturday": {"enabled": False},
    "sunday": {"enabled": False},
}


def test_working_hours_en_nonempty_when_hours_provided():
    out = _build_working_hours_section(_SAMPLE_HOURS, "America/Chicago", "en")
    assert out.strip() != ""
    # English day-short labels surface in prose
    assert "Mon" in out
    assert "Closed" in out


def test_working_hours_es_renders_same_as_en():
    # 2026-06-11 collapse: the schedule block is prompt-internal data, not
    # caller-facing prose — es-locale now renders the same EN labels (the
    # LANGUAGE section's Spanish guide covers speaking days/times in Spanish).
    out = _build_working_hours_section(_SAMPLE_HOURS, "America/Chicago", "es")
    assert out.strip() != ""
    assert "Mon" in out
    assert "Closed" in out
    assert out == _build_working_hours_section(_SAMPLE_HOURS, "America/Chicago", "en")


def test_working_hours_empty_returns_empty_en():
    assert _build_working_hours_section(None, "America/Chicago", "en") == ""


def test_working_hours_empty_returns_empty_es():
    assert _build_working_hours_section(None, "America/Chicago", "es") == ""


# --- greeting -----------------------------------------------------------------


def test_greeting_en_references_disclosure_without_inlining():
    # Phase 66: the disclosure is spoken by session.say from the message
    # template, not by the model. The greeting section REFERS to it (so the
    # model knows not to repeat it) but must NOT reproduce the verbatim sentence.
    t = _make_t("en")
    out = _build_greeting_section(
        "en", "Voco", onboarding_complete=True, t=t
    )
    assert "recording disclosure" in out.lower()
    assert _en["agent"]["recording_disclosure"] not in out


def test_greeting_es_references_disclosure_without_inlining():
    # 2026-06-11 collapse: es-locale section is the same EN text — the
    # invariant (refer to the disclosure, never inline either locale's
    # template sentence) keeps both non-inlining checks.
    t = _make_t("es")
    out = _build_greeting_section(
        "es", "Voco", onboarding_complete=True, t=t
    )
    assert "recording disclosure" in out.lower()
    assert _es["agent"]["recording_disclosure"] not in out
    assert _en["agent"]["recording_disclosure"] not in out


def test_greeting_en_es_identical():
    # 2026-06-11 collapse: the old distinctness guard inverts — the greeting
    # SECTION must not fork on locale (the spoken session.say template in
    # messages/{en,es}.json stays per-locale and untouched).
    t_en = _make_t("en")
    t_es = _make_t("es")
    out_en = _build_greeting_section("en", "Voco", True, t_en)
    out_es = _build_greeting_section("es", "Voco", True, t_es)
    assert out_en == out_es


# --- language -----------------------------------------------------------------


def test_language_en_directive():
    t = _make_t("en")
    out = _build_language_section(t, "en")
    assert "Default to English" in out


def test_language_es_directive():
    # 2026-06-11 collapse: the default-to-Spanish directive is now stated in
    # English — same invariant (locale flips the tenant default language).
    t = _make_t("es")
    out = _build_language_section(t, "es")
    assert "This business operates in Spanish" in out
    assert "default to Spanish" in out


# --- customer_account ---------------------------------------------------------


_SAMPLE_CTX = {
    "customer_name": "Jane Doe",
    "_sources": {"customer_name": "jobber"},
}


def test_customer_account_en_header_when_context_present():
    # 2026-06 emphasis trim: this section was de-inflated from a CRITICAL RULE
    # header to a plain "CUSTOMER CONTEXT:" header (its body privacy rules are
    # unchanged). Assert the header renders when context is present.
    out = _build_customer_account_section(_SAMPLE_CTX, "en")
    assert "CUSTOMER CONTEXT" in out


def test_customer_account_es_header_when_context_present():
    # 2026-06-11 collapse: es-locale output is the same EN frame — invariant
    # (section renders for es-locale calls when context present) maps to EN.
    out = _build_customer_account_section(_SAMPLE_CTX, "es")
    assert "CUSTOMER CONTEXT" in out


def test_customer_account_empty_when_no_context_en():
    assert _build_customer_account_section(None, "en") == ""
    assert _build_customer_account_section({}, "en") == ""


def test_customer_account_empty_when_no_context_es():
    assert _build_customer_account_section(None, "es") == ""
    assert _build_customer_account_section({}, "es") == ""


# --- intake_questions ---------------------------------------------------------


def test_intake_en_preamble():
    out = _build_intake_questions_section("Ask about pets", "en")
    lowered = out.lower()
    assert "additional questions" in lowered


def test_intake_es_preamble():
    # 2026-06-11 collapse: es-locale preamble is the same EN text.
    out = _build_intake_questions_section("Ask about pets", "es")
    lowered = out.lower()
    assert "additional questions" in lowered


def test_intake_empty_when_no_questions_en():
    assert _build_intake_questions_section("", "en") == ""
    assert _build_intake_questions_section(None, "en") == ""


def test_intake_empty_when_no_questions_es():
    assert _build_intake_questions_section("", "es") == ""
    assert _build_intake_questions_section(None, "es") == ""


# --- decline_handling ---------------------------------------------------------


def test_decline_en_nonempty_onboarded():
    out = _build_decline_handling_section("Voco", "en")
    assert out.strip() != ""
    assert "Voco" in out


def test_decline_es_nonempty_onboarded():
    out = _build_decline_handling_section("Voco", "es")
    assert out.strip() != ""
    assert "Voco" in out


def test_decline_en_es_identical():
    # 2026-06-11 collapse: the old distinctness guard inverts — must not fork.
    out_en = _build_decline_handling_section("Voco", "en")
    out_es = _build_decline_handling_section("Voco", "es")
    assert out_en == out_es


# --- transfer -----------------------------------------------------------------


def test_transfer_en_two_triggers():
    out = _build_transfer_section("Voco", "en")
    lowered = out.lower()
    assert "explicitly asks" in lowered
    assert ("3 attempts" in lowered) or ("three attempts" in lowered)


def test_transfer_es_two_triggers():
    # 2026-06-11 collapse: same two-trigger invariant for es-locale calls;
    # EN pins.
    out = _build_transfer_section("Voco", "es")
    lowered = out.lower()
    assert "explicitly asks" in lowered
    assert ("3 attempts" in lowered) or ("three attempts" in lowered)


# --- Global full-assembled regression guard ----------------------------------


def test_full_assembled_prompt_es_is_single_english_prompt():
    """
    2026-06-11 single-prompt collapse: the old guard (assembled ES prompt
    contains Spanish prose) INVERTS — the es-locale prompt must now be the
    single English prompt carrying only the Spanish-default line and the
    Spanish delivery guide, with no resurrected ES section headers.
    """
    prompt = build_system_prompt(
        locale="es",
        business_name="Voco",
        onboarding_complete=True,
        intake_questions="Preguntar sobre mascotas",
        country="US",
        working_hours=_SAMPLE_HOURS,
        customer_context=None,
    )
    # locale="es" effect: the default-to-Spanish line + the Spanish guide.
    assert "This business operates in Spanish" in prompt
    assert "SPEAKING SPANISH — DELIVERY GUIDE:" in prompt
    # No ES section headers may come back (anti-fork lock).
    for es_header in (
        "IDIOMA:",
        "RESERVA:",
        "PALABRAS DE RESULTADO",
        "VALIDACIÓN DE DIRECCIÓN",
        "NARRACIÓN DE HERRAMIENTAS:",
        "FINAL — INNEGOCIABLES",
    ):
        assert es_header not in prompt, (
            f"ES section header {es_header!r} resurfaced — the prompt must "
            "stay single-language English"
        )


def test_full_assembled_prompt_en_stays_english():
    """Parity guard — EN assembly must not leak Spanish prose."""
    prompt = build_system_prompt(
        locale="en",
        business_name="Voco",
        onboarding_complete=True,
        intake_questions="Ask about pets",
        country="US",
        working_hours=_SAMPLE_HOURS,
        customer_context=None,
    )
    # Must contain anchor English phrases from tail sections
    assert "CRITICAL RULE" in prompt
    assert "Default to English" in prompt
    assert "BUSINESS HOURS" in prompt
