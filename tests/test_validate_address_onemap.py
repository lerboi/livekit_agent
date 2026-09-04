"""P1.4 (2026-09-04): Singapore postal-first address resolution via OneMap.

For an SG tenant with a 6-digit postal code the validate_address tool resolves
the building from the postal (OneMap) BEFORE — instead of — Google, shapes the
hit exactly like google_maps._voco_result, and falls through to the unchanged
STATE logic. Every miss / error / non-SG / non-6-digit / flag-off case must
take today's Google path (asserted via the patched Google mock being awaited).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tools.validate_address import (
    create_validate_address_tool,
    get_cached_validation,
)
from tests.test_validate_address_tool import (
    FORMATTED,
    _bounded_result,
    _make_deps,
    _raw_args,
    patched_validate,  # noqa: F401 — fixture re-export
)


ONEMAP_HIT = {
    "SEARCHVAL": "YISHUN SAPPHIRE",
    "BLK_NO": "40",
    "ROAD_NAME": "CANBERRA DRIVE",
    "BUILDING": "YISHUN SAPPHIRE",
    "ADDRESS": "40 CANBERRA DRIVE YISHUN SAPPHIRE SINGAPORE 768433",
    "POSTAL": "768433",
    "LATITUDE": "1.4342",
    "LONGITUDE": "103.8302",
}


@pytest.fixture
def patched_onemap():
    with patch(
        "src.tools.validate_address.lookup_postal", new_callable=AsyncMock,
    ) as mock_lookup:
        yield mock_lookup


def _sg_deps(**overrides):
    return _make_deps(country="SG", **overrides)


# -- hits ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sg_postal_hit_spoken_street_differs_returns_corrected(
    patched_validate, patched_onemap
):
    """The Canberra/Kenboro case: the postal resolves the building, the spoken
    street disagrees -> confirmed_with_changes -> STATE:address_corrected with
    the OneMap form, and Google is never called."""
    patched_onemap.return_value = dict(ONEMAP_HIT)
    deps = _sg_deps()
    tool = create_validate_address_tool(deps)

    result = await tool.__wrapped__(
        _raw_args(street="Kenboro Drive", postal_code="768433", unit="07-04"), MagicMock()
    )

    assert result.startswith("STATE:address_corrected")
    assert "speech=Block 40 Canberra Drive, Yishun Sapphire" in result
    patched_validate.assert_not_awaited()
    patched_onemap.assert_awaited_once_with("768433")
    cached = deps["_validated_address"]["result"]
    assert cached["verdict"] == "confirmed_with_changes"
    assert cached["formatted_address"] == "Block 40 Canberra Drive, Yishun Sapphire"
    comps = cached["address_components"]
    assert comps["street_number"] == "40"
    assert comps["route"] == "Canberra Drive"
    assert comps["postal_code"] == "768433"
    assert comps["locality"] == "Singapore"
    assert comps["country_code"] == "SG"
    assert comps["subpremise"] == "07-04"
    assert set(comps) == {
        "street_number", "route", "subpremise", "locality", "admin_area_level_1",
        "admin_area_level_2", "postal_code", "country", "country_code",
    }
    assert cached["latitude"] == pytest.approx(1.4342)
    assert cached["longitude"] == pytest.approx(103.8302)
    assert cached["place_id"] is None
    assert deps["_last_tool_state"] == result


@pytest.mark.asyncio
async def test_sg_postal_hit_spoken_street_matches_returns_ok(
    patched_validate, patched_onemap
):
    patched_onemap.return_value = dict(ONEMAP_HIT)
    deps = _sg_deps()
    tool = create_validate_address_tool(deps)

    result = await tool.__wrapped__(
        _raw_args(street="40 Canberra Drive", postal_code="768433"), MagicMock()
    )

    assert result.startswith("STATE:address_ok ")
    assert "Block 40 Canberra Drive, Yishun Sapphire" in result
    patched_validate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("street", ["Canberra Dr", "canberra drive", "blk 40", "40"])
async def test_sg_postal_hit_block_or_fuzzy_road_counts_as_match(
    patched_validate, patched_onemap, street
):
    patched_onemap.return_value = dict(ONEMAP_HIT)
    deps = _sg_deps()
    tool = create_validate_address_tool(deps)
    result = await tool.__wrapped__(
        _raw_args(street=street, postal_code="768433"), MagicMock()
    )
    assert result.startswith("STATE:address_ok "), result


@pytest.mark.asyncio
@pytest.mark.parametrize("street", ["Burr Drive", "Kenboro Drive", "Yishun Avenue 2"])
async def test_sg_postal_hit_different_road_is_corrected(
    patched_validate, patched_onemap, street
):
    patched_onemap.return_value = dict(ONEMAP_HIT)
    deps = _sg_deps()
    tool = create_validate_address_tool(deps)
    result = await tool.__wrapped__(
        _raw_args(street=street, postal_code="768433"), MagicMock()
    )
    assert result.startswith("STATE:address_corrected"), result


@pytest.mark.asyncio
async def test_sg_postal_hit_landed_house_omits_nil_building(
    patched_validate, patched_onemap
):
    patched_onemap.return_value = {**ONEMAP_HIT, "BUILDING": "NIL", "BLK_NO": "12"}
    deps = _sg_deps()
    tool = create_validate_address_tool(deps)
    result = await tool.__wrapped__(
        _raw_args(street="12 Canberra Drive", postal_code="768433"), MagicMock()
    )
    assert "speech=Block 12 Canberra Drive |" in result
    assert "Nil" not in result and "NIL" not in result


@pytest.mark.asyncio
async def test_sg_postal_with_spaces_still_uses_onemap(patched_validate, patched_onemap):
    """The LLM converts spoken digits; 'seven six eight, four three three' may
    arrive as '768 433'."""
    patched_onemap.return_value = dict(ONEMAP_HIT)
    deps = _sg_deps()
    tool = create_validate_address_tool(deps)
    result = await tool.__wrapped__(
        _raw_args(street="Kenboro Drive", postal_code="768 433"), MagicMock()
    )
    assert result.startswith("STATE:address_corrected")
    patched_onemap.assert_awaited_once_with("768433")
    patched_validate.assert_not_awaited()


# -- fall-through to Google ------------------------------------------------------


@pytest.mark.asyncio
async def test_sg_onemap_miss_falls_through_to_google(patched_validate, patched_onemap):
    patched_onemap.return_value = None
    patched_validate.return_value = (_bounded_result("confirmed", FORMATTED), "SG")
    deps = _sg_deps()
    tool = create_validate_address_tool(deps)

    result = await tool.__wrapped__(
        _raw_args(street="Kenboro Drive", postal_code="768433"), MagicMock()
    )

    patched_onemap.assert_awaited_once()
    patched_validate.assert_awaited_once()
    assert result.startswith("STATE:address_ok ")


@pytest.mark.asyncio
async def test_sg_onemap_raising_falls_through_to_google(patched_validate, patched_onemap):
    patched_onemap.side_effect = RuntimeError("contract broken")
    patched_validate.return_value = (_bounded_result("confirmed", FORMATTED), "SG")
    deps = _sg_deps()
    tool = create_validate_address_tool(deps)
    result = await tool.__wrapped__(
        _raw_args(street="Kenboro Drive", postal_code="768433"), MagicMock()
    )
    patched_validate.assert_awaited_once()
    assert result.startswith("STATE:address_ok ")


@pytest.mark.asyncio
async def test_sg_non_six_digit_postal_goes_to_google(patched_validate, patched_onemap):
    patched_validate.return_value = (_bounded_result("unconfirmed", None), "SG")
    deps = _sg_deps()
    tool = create_validate_address_tool(deps)
    result = await tool.__wrapped__(
        _raw_args(street="Kenboro Drive", postal_code="7684"), MagicMock()
    )
    patched_onemap.assert_not_awaited()
    patched_validate.assert_awaited_once()
    assert result.startswith("STATE:address_unclear")


@pytest.mark.asyncio
async def test_sg_no_postal_goes_to_google(patched_validate, patched_onemap):
    patched_validate.return_value = (_bounded_result("unconfirmed", None), "SG")
    deps = _sg_deps()
    tool = create_validate_address_tool(deps)
    await tool.__wrapped__(_raw_args(street="Kenboro Drive", postal_code=""), MagicMock())
    patched_onemap.assert_not_awaited()
    patched_validate.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_sg_tenant_with_six_digit_postal_goes_to_google(
    patched_validate, patched_onemap
):
    patched_validate.return_value = (_bounded_result("confirmed", FORMATTED), "US")
    deps = _make_deps(country="US")
    tool = create_validate_address_tool(deps)
    await tool.__wrapped__(_raw_args(postal_code="768433"), MagicMock())
    patched_onemap.assert_not_awaited()
    patched_validate.assert_awaited_once()


@pytest.mark.asyncio
async def test_sg_onemap_disabled_by_flag_goes_to_google(
    patched_validate, patched_onemap, monkeypatch
):
    import src.tools.validate_address as va
    monkeypatch.setattr(va, "ONEMAP_ENABLED", False)
    patched_validate.return_value = (_bounded_result("confirmed", FORMATTED), "SG")
    deps = _sg_deps()
    tool = create_validate_address_tool(deps)
    await tool.__wrapped__(_raw_args(street="Kenboro Drive", postal_code="768433"), MagicMock())
    patched_onemap.assert_not_awaited()
    patched_validate.assert_awaited_once()


# -- downstream consumers keep working on the OneMap shape ----------------------


@pytest.mark.asyncio
async def test_sg_onemap_result_feeds_service_area_gate(patched_validate, patched_onemap):
    """The gate reads address_components.postal_code / .locality; the OneMap
    shape must keep SG postal-code zones working."""
    patched_onemap.return_value = dict(ONEMAP_HIT)
    deps = _sg_deps(
        _slot_cache={"service_zones": [{"postal_codes": ["768433"], "cities": []}]},
    )
    tool = create_validate_address_tool(deps)
    result = await tool.__wrapped__(
        _raw_args(street="40 Canberra Drive", postal_code="768433"), MagicMock()
    )
    assert result.startswith("STATE:address_ok ")
    assert deps["_service_area"]["verdict"] == "in_area"
    assert deps["_service_area"]["matched_on"] == "postal"


@pytest.mark.asyncio
async def test_sg_onemap_result_out_of_area_still_gates(patched_validate, patched_onemap):
    patched_onemap.return_value = dict(ONEMAP_HIT)
    deps = _sg_deps(
        _slot_cache={"service_zones": [{"postal_codes": ["018956"], "cities": []}]},
        tenant={"out_of_area_action": "callback"},
    )
    tool = create_validate_address_tool(deps)
    result = await tool.__wrapped__(
        _raw_args(street="40 Canberra Drive", postal_code="768433"), MagicMock()
    )
    assert result.startswith("STATE:address_out_of_area action=callback")


@pytest.mark.asyncio
async def test_sg_onemap_result_reused_by_cache_lookup(patched_validate, patched_onemap):
    patched_onemap.return_value = dict(ONEMAP_HIT)
    deps = _sg_deps()
    tool = create_validate_address_tool(deps)
    await tool.__wrapped__(
        _raw_args(street="40 Canberra Drive", postal_code="768433"), MagicMock()
    )
    cached = get_cached_validation(deps, "40 canberra drive ", "768433")
    assert cached is not None
    assert cached["verdict"] == "confirmed"
    assert cached["formatted_address"] == "Block 40 Canberra Drive, Yishun Sapphire"


@pytest.mark.asyncio
async def test_sg_onemap_hit_counts_toward_attempts_but_never_caps(
    patched_validate, patched_onemap
):
    """A OneMap hit is a confirmed* verdict, so the P1.3 unconfirmed cap never
    rewrites it, however many times the caller re-validates."""
    patched_onemap.return_value = dict(ONEMAP_HIT)
    deps = _sg_deps()
    tool = create_validate_address_tool(deps)
    for _ in range(3):
        result = await tool.__wrapped__(
            _raw_args(street="Kenboro Drive", postal_code="768433"), MagicMock()
        )
    assert result.startswith("STATE:address_corrected")
