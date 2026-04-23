"""Phase 60.4 Stream B — RealtimeModel language kwarg wiring tests (D-B-01).

Tests the locale → BCP-47 map and its wiring into the RealtimeModel kwarg.
The plugin accepts `language: NotGivenOr[str]`; we pass a single BCP-47
string derived from `tenants.default_locale`. Unknown/null locales fall
back to en-US.

Scope (per CONTEXT): only "en" and "es" map to distinct codes; all others
degrade to en-US.
"""
from __future__ import annotations

import pytest


def test_default_locale_en_maps_to_enUS_kwarg():
    from src.agent import _locale_to_bcp47

    assert _locale_to_bcp47("en") == "en-US"


def test_default_locale_es_maps_to_esUS():
    from src.agent import _locale_to_bcp47

    assert _locale_to_bcp47("es") == "es-US"


@pytest.mark.parametrize("val", [None, "", "zh", "ms", "ta", "vi", "unknown"])
def test_null_default_locale_falls_back_to_enUS(val):
    from src.agent import _locale_to_bcp47

    assert _locale_to_bcp47(val) == "en-US"
