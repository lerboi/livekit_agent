"""Phase 61 Plan 02 — Wave 0 contract tests for src/integrations/google_maps.py.

These tests describe the contract Plan 02 Task 2 implements. They cover:

- Verdict mapper (5 tests): Google's possibleNextAction enum → Voco 6-state verdict
- Components mapper (4 tests): Google's addressComponents[] → Voco D-D1 named-key dict
- country_code source (1 test): Pitfall 4 — read from postalAddress.regionCode
- HTTP error paths (4 tests): missing API key, 400/unsupported, 429, timeout
- Sentry gate (2 tests): capture only on verdict='error' (D-A3, D-C3)
- Telemetry (1 test): one row per validate to gmaps_validate_events (D-C2')
- Public API shape (1 test): Voco-shaped dict keys

All tests are deterministic — no live API calls, no DB writes. The httpx
client is mocked at the AsyncClient.post boundary; the supabase client is
passed in as a kwarg and substituted with a MagicMock in tests.

RED phase: every test FAILS with ModuleNotFoundError until Task 2 implements
src.integrations.google_maps.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Test setup ──────────────────────────────────────────────────────────────


def _import_module():
    """Lazy import inside each test so RED-phase ModuleNotFoundError surfaces
    with the test name (clearer than a top-level ImportError that breaks
    pytest collection).
    """
    from src.integrations import google_maps  # noqa: WPS433
    return google_maps


def _make_mock_response(status_code: int, json_body: dict) -> MagicMock:
    """Build a MagicMock that mimics httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_body)
    # text is sometimes used for error-classification (4xx body inspection)
    resp.text = json.dumps(json_body)
    return resp


# ── Verdict mapper tests (5) ────────────────────────────────────────────────


def test_map_verdict_accept_to_confirmed():
    """ACCEPT → confirmed (D-A1)."""
    gm = _import_module()
    result = gm.map_verdict({"result": {"verdict": {"possibleNextAction": "ACCEPT"}}})
    assert result == "confirmed"


def test_map_verdict_confirm_to_confirmed_with_changes():
    """CONFIRM → confirmed_with_changes (D-A1)."""
    gm = _import_module()
    result = gm.map_verdict({"result": {"verdict": {"possibleNextAction": "CONFIRM"}}})
    assert result == "confirmed_with_changes"


def test_map_verdict_confirm_add_subpremises_to_confirmed_with_changes():
    """CONFIRM_ADD_SUBPREMISES → confirmed_with_changes (D-B1 collapse — Voco
    doesn't probe for unit numbers)."""
    gm = _import_module()
    result = gm.map_verdict({"result": {"verdict": {"possibleNextAction": "CONFIRM_ADD_SUBPREMISES"}}})
    assert result == "confirmed_with_changes"


def test_map_verdict_fix_to_unconfirmed():
    """FIX → unconfirmed (D-A1)."""
    gm = _import_module()
    result = gm.map_verdict({"result": {"verdict": {"possibleNextAction": "FIX"}}})
    assert result == "unconfirmed"


def test_map_verdict_missing_action_defaults_unconfirmed():
    """Defensive: missing possibleNextAction → unconfirmed."""
    gm = _import_module()
    # No possibleNextAction key at all
    result = gm.map_verdict({"result": {"verdict": {}}})
    assert result == "unconfirmed"
    # Also handle missing verdict block entirely
    result2 = gm.map_verdict({"result": {}})
    assert result2 == "unconfirmed"
    # Also handle empty dict
    result3 = gm.map_verdict({})
    assert result3 == "unconfirmed"


# ── Components mapper tests (4) ─────────────────────────────────────────────


def test_components_mapper_us(gmaps_fixture):
    """US fixture → all 9 named keys present, country_code='US'."""
    gm = _import_module()
    fixture = gmaps_fixture("us_confirmed")
    result = gm.map_components(fixture["result"]["address"])

    # 9-key shape per D-D1
    expected_keys = {
        "street_number", "route", "subpremise",
        "locality", "admin_area_level_1", "admin_area_level_2",
        "postal_code", "country", "country_code",
    }
    assert set(result.keys()) == expected_keys

    assert result["street_number"] == "1600"
    assert result["route"] == "Amphitheatre Parkway"
    assert result["postal_code"] == "94043"
    assert result["country"] == "United States"
    assert result["country_code"] == "US"
    assert result["locality"] == "Mountain View"


def test_components_mapper_ca(gmaps_fixture):
    """CA fixture → country_code='CA'."""
    gm = _import_module()
    fixture = gmaps_fixture("ca_confirmed")
    result = gm.map_components(fixture["result"]["address"])
    assert result["country_code"] == "CA"
    assert result["country"] == "Canada"
    assert result["locality"] == "Ottawa"


def test_components_mapper_sg_hdb(gmaps_fixture):
    """SG HDB fixture with subpremise → country_code='SG', subpremise present."""
    gm = _import_module()
    fixture = gmaps_fixture("sg_hdb_confirmed")
    result = gm.map_components(fixture["result"]["address"])
    assert result["country_code"] == "SG"
    assert result["country"] == "Singapore"
    assert result["subpremise"] == "08-456"
    assert result["postal_code"] == "560123"
    # SG addresses lack `locality`; mapper should fall back to sublocality/sublocality_level_1
    assert result["locality"] in ("Ang Mo Kio",)


def test_components_mapper_sg_subpremise_absent(gmaps_fixture):
    """SG fixture without subpremise → subpremise=None (no crash)."""
    gm = _import_module()
    fixture = gmaps_fixture("sg_hdb_subpremise_missing")
    result = gm.map_components(fixture["result"]["address"])
    assert result["subpremise"] is None
    assert result["country_code"] == "SG"
    assert result["postal_code"] == "560123"


# ── country_code source test (1) — Pitfall 4 ────────────────────────────────


def test_country_code_from_region_code():
    """country_code MUST be sourced from postalAddress.regionCode, NOT from
    addressComponents (Pitfall 4). Even if addressComponents has only the
    long-form `country` text, country_code must come through."""
    gm = _import_module()
    # Construct an address block where addressComponents has the long-form
    # country name but no short_name (matches Address Validation API shape)
    addr = {
        "postalAddress": {
            "regionCode": "SG",
            "languageCode": "en",
            "postalCode": "560123",
        },
        "addressComponents": [
            {
                "componentName": {"text": "Singapore", "languageCode": "en"},
                "componentType": "country",
                "confirmationLevel": "CONFIRMED",
            },
            {
                "componentName": {"text": "560123", "languageCode": "en"},
                "componentType": "postal_code",
                "confirmationLevel": "CONFIRMED",
            },
        ],
    }
    result = gm.map_components(addr)
    # country_code MUST come from postalAddress.regionCode
    assert result["country_code"] == "SG"
    # country (long form) comes from addressComponents
    assert result["country"] == "Singapore"


# ── HTTP error path tests (4) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_address_bounded_returns_skipped_when_no_api_key(monkeypatch):
    """Missing GOOGLE_MAPS_API_KEY → verdict='skipped' (D-G1)."""
    gm = _import_module()
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)

    result = await gm.validate_address_bounded(
        tenant_id="t1",
        call_id="c1",
        region_code="US",
        address_lines=["1600 Amphitheatre Pkwy"],
        supabase=None,
    )
    assert result["verdict"] == "skipped"


@pytest.mark.asyncio
async def test_validate_address_bounded_returns_unsupported_region_on_400(
    monkeypatch, gmaps_fixture
):
    """HTTP 400 with INVALID_ARGUMENT/regionCode → verdict='unsupported_region' (D-A3).

    Uses region_code='US' (a SUPPORTED region) so the input-region short-circuit
    does NOT fire; this exercises the `_is_unsupported_region_400` body-match path
    (Google itself returns a region 400 even for a region we accept as input).
    """
    gm = _import_module()
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")

    error_body = gmaps_fixture("unsupported_region_de")
    mock_resp = _make_mock_response(400, error_body)

    with patch("httpx.AsyncClient.post", AsyncMock(return_value=mock_resp)):
        result = await gm.validate_address_bounded(
            tenant_id="t1",
            call_id="c1",
            region_code="US",
            address_lines=["Some street"],
            supabase=None,
        )
    assert result["verdict"] == "unsupported_region"


@pytest.mark.asyncio
async def test_validate_address_bounded_returns_error_on_429(
    monkeypatch, gmaps_fixture
):
    """HTTP 429 (quota exceeded) → verdict='error'."""
    gm = _import_module()
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")

    error_body = gmaps_fixture("quota_exceeded_429")
    mock_resp = _make_mock_response(429, error_body)

    with patch("httpx.AsyncClient.post", AsyncMock(return_value=mock_resp)):
        result = await gm.validate_address_bounded(
            tenant_id="t1",
            call_id="c1",
            region_code="US",
            address_lines=["1600 Amphitheatre Pkwy"],
            supabase=None,
        )
    assert result["verdict"] == "error"


@pytest.mark.asyncio
async def test_validate_address_bounded_returns_error_on_timeout(monkeypatch):
    """asyncio.TimeoutError → verdict='error', latency_ms ~ 1500."""
    gm = _import_module()
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")

    async def _slow_post(*args, **kwargs):
        await asyncio.sleep(10)  # exceeds the 1.5s budget; will raise TimeoutError
        return MagicMock()

    with patch("httpx.AsyncClient.post", new=_slow_post):
        result = await gm.validate_address_bounded(
            tenant_id="t1",
            call_id="c1",
            region_code="US",
            address_lines=["1600 Amphitheatre Pkwy"],
            supabase=None,
            timeout_seconds=0.1,  # speed up the test
        )
    assert result["verdict"] == "error"
    # latency_ms should reflect the timeout budget (within tolerance)
    assert result["latency_ms"] >= 50  # at least the timeout
    assert result["latency_ms"] < 5000  # but not absurdly long


# ── Sentry gate tests (2) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sentry_called_only_on_error_verdict(monkeypatch, gmaps_fixture):
    """500 response → verdict='error' → sentry_sdk.capture_exception called once."""
    gm = _import_module()
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")

    error_body = {"error": {"code": 500, "message": "Internal", "status": "INTERNAL"}}
    mock_resp = _make_mock_response(500, error_body)

    with patch("httpx.AsyncClient.post", AsyncMock(return_value=mock_resp)), \
         patch.object(gm, "sentry_sdk") as mock_sentry:
        mock_sentry.capture_exception = MagicMock()
        result = await gm.validate_address_bounded(
            tenant_id="t1",
            call_id="c1",
            region_code="US",
            address_lines=["1600 Amphitheatre Pkwy"],
            supabase=None,
        )

    assert result["verdict"] == "error"
    assert mock_sentry.capture_exception.call_count == 1


@pytest.mark.asyncio
async def test_sentry_NOT_called_on_unsupported_region(monkeypatch, gmaps_fixture):
    """unsupported_region MUST NOT page Sentry (D-A3, D-C3)."""
    gm = _import_module()
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")

    error_body = gmaps_fixture("unsupported_region_de")
    mock_resp = _make_mock_response(400, error_body)

    with patch("httpx.AsyncClient.post", AsyncMock(return_value=mock_resp)), \
         patch.object(gm, "sentry_sdk") as mock_sentry:
        mock_sentry.capture_exception = MagicMock()
        result = await gm.validate_address_bounded(
            tenant_id="t1",
            call_id="c1",
            region_code="US",
            address_lines=["Some street"],
            supabase=None,
        )

    assert result["verdict"] == "unsupported_region"
    assert mock_sentry.capture_exception.call_count == 0


@pytest.mark.asyncio
async def test_validate_address_skipped_for_unsupported_region(monkeypatch):
    """An unsupported INPUT region (e.g. DE) → verdict='skipped' with NO HTTP call.

    The input-region short-circuit fires before any network I/O, so httpx.AsyncClient.post
    must never be invoked (no billing, no Sentry, no latency).
    """
    gm = _import_module()
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")

    mock_post = AsyncMock()

    with patch("httpx.AsyncClient.post", mock_post):
        result = await gm.validate_address_bounded(
            tenant_id="t1",
            call_id="c1",
            region_code="DE",
            address_lines=["Some valid street 123"],
            supabase=None,
        )

    assert result["verdict"] == "skipped"
    assert mock_post.call_count == 0


# ── Telemetry test (1) — D-C2' ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_telemetry_row_inserted_per_call(monkeypatch, gmaps_fixture):
    """validate_address_bounded inserts ONE row to gmaps_validate_events with
    verdict + latency_ms + region_code + tenant_id (D-C2')."""
    gm = _import_module()
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")

    success_body = gmaps_fixture("us_confirmed")
    mock_resp = _make_mock_response(200, success_body)

    # Mock the supabase client chain: supabase.table('...').insert({...}).execute()
    mock_execute = MagicMock(return_value=MagicMock(data=[{"id": "row-1"}]))
    mock_insert = MagicMock(return_value=MagicMock(execute=mock_execute))
    mock_table = MagicMock(return_value=MagicMock(insert=mock_insert))
    mock_supabase = MagicMock()
    mock_supabase.table = mock_table

    with patch("httpx.AsyncClient.post", AsyncMock(return_value=mock_resp)):
        await gm.validate_address_bounded(
            tenant_id="t1",
            call_id="c1",
            region_code="US",
            address_lines=["1600 Amphitheatre Pkwy"],
            supabase=mock_supabase,
        )

    # Telemetry contract: one .table('gmaps_validate_events').insert(...) call
    assert mock_table.called, "supabase.table(...) was not called"
    # Find the gmaps_validate_events call (the only table called from this path)
    table_calls = [c.args[0] for c in mock_table.call_args_list]
    assert "gmaps_validate_events" in table_calls

    # The insert payload must include verdict + latency_ms + region_code + tenant_id
    assert mock_insert.called, "insert(...) was not called"
    insert_payload = mock_insert.call_args.args[0]
    assert "verdict" in insert_payload
    assert "latency_ms" in insert_payload
    assert "region_code" in insert_payload
    assert "tenant_id" in insert_payload
    assert insert_payload["tenant_id"] == "t1"
    assert insert_payload["region_code"] == "US"
    assert insert_payload["verdict"] == "confirmed"


# ── Public API shape test (1) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_address_bounded_return_dict_keys(monkeypatch, gmaps_fixture):
    """Any successful path → returned dict has all Voco-shaped keys."""
    gm = _import_module()
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")

    success_body = gmaps_fixture("us_confirmed")
    mock_resp = _make_mock_response(200, success_body)

    with patch("httpx.AsyncClient.post", AsyncMock(return_value=mock_resp)):
        result = await gm.validate_address_bounded(
            tenant_id="t1",
            call_id="c1",
            region_code="US",
            address_lines=["1600 Amphitheatre Pkwy"],
            supabase=None,
        )

    expected_keys = {
        "verdict",
        "formatted_address",
        "place_id",
        "latitude",
        "longitude",
        "address_components",
        "latency_ms",
    }
    missing = expected_keys - set(result.keys())
    assert not missing, f"Result missing keys: {missing}; got {set(result.keys())}"

    # Sanity: on success, formatted_address + place_id should be populated
    assert result["verdict"] == "confirmed"
    assert result["formatted_address"] == "1600 Amphitheatre Pkwy, Mountain View, CA 94043, USA"
    assert result["place_id"] == "ChIJ2eUgeAK6j4ARbn5u_wAGqWA"
    assert result["address_components"]["country_code"] == "US"


# ── Additional contract tests beyond the minimum 17 ─────────────────────────


def test_module_exports_constants():
    """Public API: VERDICT_* constants must be exported."""
    gm = _import_module()
    assert gm.VERDICT_ACCEPT == "ACCEPT"
    assert gm.VERDICT_CONFIRM == "CONFIRM"
    assert gm.VERDICT_CONFIRM_ADD_SUBPREMISES == "CONFIRM_ADD_SUBPREMISES"
    assert gm.VERDICT_FIX == "FIX"


@pytest.mark.asyncio
async def test_validate_address_bounded_never_raises_on_unexpected_exception(
    monkeypatch,
):
    """Outer wrapper MUST never raise — any exception → verdict='error'."""
    gm = _import_module()
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")

    async def _broken_post(*args, **kwargs):
        raise RuntimeError("kaboom — simulated network failure")

    with patch("httpx.AsyncClient.post", new=_broken_post):
        # Must not raise — must return a Voco-shaped error dict
        result = await gm.validate_address_bounded(
            tenant_id="t1",
            call_id="c1",
            region_code="US",
            address_lines=["1600 Amphitheatre Pkwy"],
            supabase=None,
        )
    assert result["verdict"] == "error"
    assert "latency_ms" in result


# ── Phase 61.1 WR-01: tenant_id falsy must skip telemetry insert ────────────


def test_telemetry_skipped_when_tenant_id_none(monkeypatch):
    """WR-01 (Phase 61.1): tenant_id=None must skip the insert with a warn log,
    NOT swallow a NOT NULL constraint violation in a bare except."""
    import asyncio
    from unittest.mock import MagicMock
    # Ensure the API-key skip-fast path so we don't actually call Google.
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)

    mock_supabase = MagicMock()
    from src.integrations.google_maps import validate_address_bounded

    result = asyncio.run(
        validate_address_bounded(
            tenant_id=None,
            call_id="call-x",
            region_code="US",
            address_lines=["1600 Amphitheatre Pkwy"],
            supabase=mock_supabase,
        )
    )
    assert result["verdict"] == "skipped"
    # The insert path must NEVER be reached when tenant_id is falsy.
    mock_supabase.table.assert_not_called()


def test_telemetry_skipped_when_tenant_id_empty_string(monkeypatch):
    """WR-01 (Phase 61.1): empty-string tenant_id treated identically to None."""
    import asyncio
    from unittest.mock import MagicMock
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)

    mock_supabase = MagicMock()
    from src.integrations.google_maps import validate_address_bounded

    result = asyncio.run(
        validate_address_bounded(
            tenant_id="",
            call_id="call-x",
            region_code="US",
            address_lines=["1600 Amphitheatre Pkwy"],
            supabase=mock_supabase,
        )
    )
    assert result["verdict"] == "skipped"
    mock_supabase.table.assert_not_called()


# ── Phase 61.1 WR-02: empty address_lines must short-circuit before HTTP ────


def test_empty_address_lines_short_circuits_to_error(monkeypatch):
    """WR-02 (Phase 61.1): empty address_lines must short-circuit to verdict=error
    BEFORE the HTTP call. Otherwise Google returns 400 INVALID_ARGUMENT and the
    response is misclassified as verdict=unsupported_region (false billing
    rollup + lost Sentry signal)."""
    import asyncio
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "fake-key-must-not-be-used")

    from src.integrations.google_maps import validate_address

    # Empty list — must short-circuit before httpx.AsyncClient is constructed.
    result = asyncio.run(
        validate_address(region_code="US", address_lines=[])
    )
    assert result["verdict"] == "error"
    assert result["raw_status"] is None  # short-circuit path: no HTTP issued


def test_whitespace_only_address_lines_short_circuits_to_error(monkeypatch):
    """WR-02 (Phase 61.1): whitespace-only address_lines (e.g. ['', '  '])
    are equivalent to empty for upstream-capture purposes — must also short-circuit."""
    import asyncio
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "fake-key-must-not-be-used")

    from src.integrations.google_maps import validate_address

    result = asyncio.run(
        validate_address(region_code="US", address_lines=["", "   "])
    )
    assert result["verdict"] == "error"
    assert result["raw_status"] is None


def test_empty_address_lines_triggers_sentry_via_wrapper(monkeypatch):
    """WR-02 (Phase 61.1): the bounded wrapper's Sentry gate (D-A3) must fire
    for empty-address calls because they are now verdict=error. Restores the
    'we never captured an address' alerting signal."""
    import asyncio
    from unittest.mock import patch
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "fake-key-must-not-be-used")

    from src.integrations.google_maps import validate_address_bounded

    with patch("src.integrations.google_maps.sentry_sdk") as mock_sentry:
        result = asyncio.run(
            validate_address_bounded(
                tenant_id="tenant-x",
                call_id="call-x",
                region_code="US",
                address_lines=[],
            )
        )
        assert result["verdict"] == "error"
        # Sentry MUST be called for verdict=error (D-A3 gate).
        mock_sentry.capture_exception.assert_called_once()
