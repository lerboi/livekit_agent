"""Phase 60.3 Plan 10 — _build_info_gathering_section invariant lock.

Stream B patch addresses 60.3-PROMPT-AUDIT.md §_build_info_gathering_section
(dimensions D6 token economy ✗, D7 locale parity maintenance ✓).

This section already carried an `if locale == "es":` branch for the inner
name_use and service_address blocks prior to Phase 60.3 — the gold-standard
locale-parity pattern. This plan's job:

- **Lock** Phase 60's single-question-at-a-time framing (Phase 60 lockdown)
  so future edits can't drift the EN or ES invariants.
- **Maintain** locale parity: no regression on the existing es branch;
  compress the outer-frame preamble symmetrically across EN and ES.
- **Apply** the D6 compression target flagged by the audit (remove the
  "This applies in every language" clause — redundant with
  `_build_language_section`).
- **Verify** postal_label parametrization works in both locales (SG → "postal
  code"; US → "zip code").

Invariants asserted here:
1. Phase 60 single-question framing present in EN ("one" + "at a time") and
   ES ("una" + "a la vez" OR "por vez").
2. Address intake surface present in both locales (EN "address",
   ES "dirección").
3. Phone-readback invariant present in both locales — callers' phone
   numbers must be read back / confirmed, never fabricated.
4. EN and ES outputs are distinct and non-empty (parity guard).
5. EN and ES lengths within 30% of each other (prevents one-sided drift
   during future edits).
6. postal_label ("postal code" / "zip code") surfaces correctly in EN
   and ES address blocks.
"""
from __future__ import annotations

import pytest

from src.prompt import _build_info_gathering_section


def _noop_t(key: str) -> str:
    """Minimal translator stub — returns the key itself.

    _build_info_gathering_section does not consume any translator keys in
    its body (prose is inline), but the signature accepts `t` positionally
    so downstream builders can evolve without breaking callers.
    """
    return key


# ---------------------------------------------------------------------------
# Phase 60 single-question-at-a-time framing (lockdown)
# ---------------------------------------------------------------------------


def test_en_exists_single_question_framing():
    section = _build_info_gathering_section(_noop_t, "postal code", "en")
    assert isinstance(section, str)
    assert section
    lowered = section.lower()
    assert "one" in lowered
    assert "at a time" in lowered


def test_es_exists_single_question_framing():
    section = _build_info_gathering_section(_noop_t, "código postal", "es")
    assert isinstance(section, str)
    assert section
    lowered = section.lower()
    assert "una" in lowered
    assert ("a la vez" in lowered) or ("por vez" in lowered)


# ---------------------------------------------------------------------------
# Address intake surface
# ---------------------------------------------------------------------------


def test_en_address_intake_present():
    section = _build_info_gathering_section(_noop_t, "postal code", "en")
    assert "address" in section.lower()


def test_es_address_intake_present():
    section = _build_info_gathering_section(_noop_t, "código postal", "es")
    assert "dirección" in section.lower()


# ---------------------------------------------------------------------------
# Phone-readback invariant (both locales must carry it)
# ---------------------------------------------------------------------------


def test_en_phone_readback_present():
    section = _build_info_gathering_section(_noop_t, "postal code", "en")
    lowered = section.lower()
    assert "phone number" in lowered
    assert ("read back" in lowered) or ("confirm" in lowered)


def test_es_phone_readback_present():
    section = _build_info_gathering_section(_noop_t, "código postal", "es")
    lowered = section.lower()
    assert "número de teléfono" in lowered
    assert "confirmar" in lowered


# ---------------------------------------------------------------------------
# Parity guards
# ---------------------------------------------------------------------------


def test_en_es_distinct_and_nonempty():
    en = _build_info_gathering_section(_noop_t, "postal code", "en")
    es = _build_info_gathering_section(_noop_t, "código postal", "es")
    assert en
    assert es
    assert en != es


def test_en_es_length_within_1pt5x():
    en = _build_info_gathering_section(_noop_t, "postal code", "en")
    es = _build_info_gathering_section(_noop_t, "código postal", "es")
    # Catches drift where one locale gets trimmed but the other doesn't.
    # Spanish prose is typically ~15-25% longer than equivalent English;
    # 30% tolerance is generous.
    delta = abs(len(en) - len(es)) / max(len(en), len(es))
    assert delta < 0.30, f"EN/ES length drift exceeds 30%: delta={delta:.3f}"


# ---------------------------------------------------------------------------
# postal_label parametrization (SG vs US market)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "en_label,es_label",
    [
        ("postal code", "código postal"),  # SG market
        ("zip code", "código postal"),     # US market — ES label is locale-neutral
    ],
)
def test_postal_label_parametrized_both_locales(en_label: str, es_label: str):
    en = _build_info_gathering_section(_noop_t, en_label, "en")
    es = _build_info_gathering_section(_noop_t, es_label, "es")
    # EN carries the passed label literally.
    assert en_label in en, f"EN section missing postal_label {en_label!r}"
    # ES uses its own postal phrasing ("código postal") regardless of market.
    # The test accepts the Spanish form since postal_label is the English
    # surface form; ES prose uses "código postal" idiomatically.
    assert "código postal" in es.lower()
