"""Phase 63.1 Plan 01 Task 1 — RED tests for opening-greeting directive.

Locks the contract that `_build_greeting_section` must emit an outcome-shaped
greeting directive (EN + ES parity) that tells the model the FIRST thing the
caller hears is a warm business-branded greeting — the replacement mechanism
for the deleted `session.generate_reply("Greet the caller now.")` call site
at `src/agent.py:755`.

Style: Phase 60.3 Plan 12 inverted-substring assertions (lowercased),
outcome-shaped (no verbatim scripts), locale-parity required.

Assertions are scoped to `_build_greeting_section` output (not the full
assembled prompt) to avoid false-RED/false-GREEN signal from unrelated
sections (e.g., Phase 60.3 Plan 03 `_build_call_duration_section` carries
a `"Thank you for calling Voco"` WRONG-example string — that is NOT the
greeting directive this test targets).

All tests fail RED today (directive text is not yet in
`_build_greeting_section`). They flip GREEN only after Plan 03 adds the
greeting directive in both locales.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.prompt import _build_greeting_section

_messages_dir = Path(__file__).parent.parent / "src" / "messages"


def _load(name: str) -> dict:
    with open(_messages_dir / f"{name}.json", "r", encoding="utf-8") as f:
        return json.load(f)


_en = _load("en")
_es = _load("es")


def _mk_t(msgs: dict):
    def t(key: str) -> str:
        val: object = msgs
        for part in key.split("."):
            if isinstance(val, dict):
                val = val.get(part)
            else:
                return key
        return val if isinstance(val, str) else key

    return t


def _greeting_en(business_name: str = "AcmeCorp", onboarding_complete: bool = True) -> str:
    return _build_greeting_section("en", business_name, onboarding_complete, _mk_t(_en))


def _greeting_es(business_name: str = "AcmeCorp", onboarding_complete: bool = True) -> str:
    return _build_greeting_section("es", business_name, onboarding_complete, _mk_t(_es))


def test_greeting_directive_en_states_first_thing_contract():
    out = _greeting_en().lower()
    assert "first thing" in out
    assert "branded greeting" in out


def test_greeting_directive_es_states_first_thing_contract():
    out = _greeting_es().lower()
    assert "lo primero" in out
    assert "saludo" in out
    assert "con marca" in out


def test_greeting_directive_interpolates_business_name_en():
    out = _greeting_en(business_name="AcmeCorp").lower()
    assert "acmecorp" in out


def test_greeting_directive_interpolates_business_name_es():
    out = _greeting_es(business_name="AcmeCorp").lower()
    assert "acmecorp" in out


def test_greeting_directive_no_verbatim_script_en():
    # Outcome-shaped, not a script: greeting section must contain the
    # "first thing ... branded greeting" directive (Plan 03) AND must NOT
    # pin the model to a verbatim phrase. Conjoined so this fails RED today
    # (no directive yet) and flips GREEN only when Plan 03 adds the
    # outcome-shaped directive without introducing a verbatim script.
    out = _greeting_en().lower()
    assert "branded greeting" in out  # RED today — directive not yet present
    assert "thank you for calling" not in out


def test_greeting_directive_no_verbatim_script_es():
    out = _greeting_es().lower()
    assert "con marca" in out  # RED today — directive not yet present
    assert "gracias por llamar" not in out
