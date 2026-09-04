"""Tests for src.integrations.onemap (P1.4, 2026-09-04).

The lookup is never-raising and only fires for a 6-digit postal code. Every
failure mode (timeout, transport error, HTTP 5xx, malformed JSON, no results,
wrong-postal result) returns None so the validate_address tool falls through
to the Google path exactly as before.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.integrations import onemap


ONEMAP_HIT = {
    "found": 1,
    "totalNumPages": 1,
    "pageNum": 1,
    "results": [
        {
            "SEARCHVAL": "YISHUN SAPPHIRE",
            "BLK_NO": "40",
            "ROAD_NAME": "CANBERRA DRIVE",
            "BUILDING": "YISHUN SAPPHIRE",
            "ADDRESS": "40 CANBERRA DRIVE YISHUN SAPPHIRE SINGAPORE 768433",
            "POSTAL": "768433",
            "LATITUDE": "1.4342",
            "LONGITUDE": "103.8302",
        }
    ],
}


def _resp(status=200, json_data=None, json_exc=None):
    def _json():
        if json_exc:
            raise json_exc
        return json_data
    return SimpleNamespace(status_code=status, json=_json)


def _patch_get(return_value=None, side_effect=None):
    return patch(
        "httpx.AsyncClient.get",
        new_callable=AsyncMock,
        return_value=return_value,
        side_effect=side_effect,
    )


# -- postal guard --------------------------------------------------------------

@pytest.mark.parametrize("postal,expected", [
    ("768433", True),
    ("768 433", True),
    ("768-433", True),
    ("94043", False),
    ("7684331", False),
    ("", False),
    (None, False),
    ("76843a", False),
])
def test_is_sg_postal(postal, expected):
    assert onemap.is_sg_postal(postal) is expected


async def test_non_six_digit_postal_short_circuits_without_network():
    with _patch_get(return_value=_resp(200, ONEMAP_HIT)) as get:
        assert await onemap.lookup_postal("94043") is None
        assert await onemap.lookup_postal("") is None
    assert get.await_count == 0


# -- hit -------------------------------------------------------------------------

async def test_hit_returns_first_result_and_sends_verified_params():
    with _patch_get(return_value=_resp(200, ONEMAP_HIT)) as get:
        hit = await onemap.lookup_postal("768433")
    assert hit == ONEMAP_HIT["results"][0]
    args, kwargs = get.await_args
    assert args[0] == onemap.ONEMAP_SEARCH_URL
    assert kwargs["params"]["searchVal"] == "768433"
    assert kwargs["params"]["getAddrDetails"] == "Y"
    assert kwargs["params"]["pageNum"] == "1"


async def test_hit_normalizes_spaced_postal_before_query():
    with _patch_get(return_value=_resp(200, ONEMAP_HIT)) as get:
        hit = await onemap.lookup_postal("768 433")
    assert hit is not None
    assert get.await_args.kwargs["params"]["searchVal"] == "768433"


# -- miss / failure paths never raise ---------------------------------------------

async def test_miss_returns_none():
    with _patch_get(return_value=_resp(200, {"found": 0, "results": []})):
        assert await onemap.lookup_postal("000000") is None


async def test_wrong_postal_in_first_result_is_ignored():
    wrong = {"found": 1, "results": [{**ONEMAP_HIT["results"][0], "POSTAL": "768434"}]}
    with _patch_get(return_value=_resp(200, wrong)):
        assert await onemap.lookup_postal("768433") is None


async def test_timeout_returns_none():
    with _patch_get(side_effect=httpx.ReadTimeout("slow")):
        assert await onemap.lookup_postal("768433") is None


async def test_transport_error_returns_none():
    with _patch_get(side_effect=httpx.ConnectError("boom")):
        assert await onemap.lookup_postal("768433") is None


async def test_http_500_returns_none():
    with _patch_get(return_value=_resp(500, {"error": "x"})):
        assert await onemap.lookup_postal("768433") is None


async def test_malformed_json_returns_none():
    with _patch_get(return_value=_resp(200, json_exc=ValueError("not json"))):
        assert await onemap.lookup_postal("768433") is None


async def test_unexpected_shape_returns_none():
    with _patch_get(return_value=_resp(200, ["not", "a", "dict"])):
        assert await onemap.lookup_postal("768433") is None
    with _patch_get(return_value=_resp(200, {"results": "nope"})):
        assert await onemap.lookup_postal("768433") is None
