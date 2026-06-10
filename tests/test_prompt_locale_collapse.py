"""2026-06-11 single-prompt collapse — structural equivalence lock.

The prompt is single-language English; `locale` drives exactly ONE thing:
the tenant-default-language line in the LANGUAGE section. This test builds
the en and es prompts under several representative configs and asserts they
differ in EXACTLY that one line — so nobody can quietly reintroduce an
`if locale == "es"` fork in any section builder without this failing.
"""
from __future__ import annotations

import pytest

from src.prompt import build_system_prompt

_WORKING_HOURS = {
    "monday": {"enabled": True, "open": "09:00", "close": "17:00",
               "lunchStart": "12:00", "lunchEnd": "13:00"},
    "tuesday": {"enabled": True, "open": "09:00", "close": "17:00"},
    "wednesday": {"enabled": True, "open": "09:00", "close": "17:00"},
    "thursday": {"enabled": True, "open": "09:00", "close": "17:00"},
    "friday": {"enabled": True, "open": "09:00", "close": "17:00"},
    "saturday": {"enabled": False},
    "sunday": {"enabled": False},
}

_EN_DEFAULT_LINE_START = "Default to English on every call."
_ES_DEFAULT_LINE_START = (
    "This business operates in Spanish — open in Spanish and default to "
    "Spanish on every call."
)


@pytest.mark.parametrize(
    "kwargs",
    [
        # onboarded, hours + intake (US)
        dict(
            business_name="Ace Plumbing",
            onboarding_complete=True,
            tone_preset="friendly",
            intake_questions="Is the water shut off?",
            country="US",
            working_hours=_WORKING_HOURS,
            tenant_timezone="America/Chicago",
        ),
        # minimal / not onboarded — all defaults
        dict(),
        # SG market label path
        dict(
            business_name="Voco",
            onboarding_complete=True,
            country="SG",
            tenant_timezone="Asia/Singapore",
        ),
    ],
    ids=["onboarded_us", "minimal", "onboarded_sg"],
)
def test_en_es_prompts_differ_only_in_default_language_line(kwargs):
    en = build_system_prompt("en", **kwargs)
    es = build_system_prompt("es", **kwargs)

    en_lines = en.splitlines()
    es_lines = es.splitlines()

    # Same structure: identical section/line count and order.
    assert len(en_lines) == len(es_lines), (
        "en/es prompts have different line counts — a section is forking "
        "on locale"
    )

    diffs = [
        (i, a, b) for i, (a, b) in enumerate(zip(en_lines, es_lines)) if a != b
    ]
    assert len(diffs) == 1, (
        f"en/es prompts must differ in exactly ONE line (the tenant-default-"
        f"language line); got {len(diffs)} differing lines: "
        f"{[(i, a[:60], b[:60]) for i, a, b in diffs]}"
    )

    _, en_line, es_line = diffs[0]
    assert en_line.startswith(_EN_DEFAULT_LINE_START)
    assert es_line.startswith(_ES_DEFAULT_LINE_START)
    # And the remainder of that LANGUAGE paragraph is shared verbatim.
    assert en_line.removeprefix(_EN_DEFAULT_LINE_START) == es_line.removeprefix(
        _ES_DEFAULT_LINE_START
    )
