"""Phase 61 Plan 03 — capture_lead integration tests for D-B4 + D-D3' + D-E2.

Symmetric to test_book_appointment_validation.py — locks the validate-then-record
flow on the unbooked path: validate_address_bounded runs BEFORE record_outcome,
formatted_address overwrites service_address ONLY on confirmed/confirmed_with_changes
verdicts, and the success-path tool return emits the verdict-driven LEAD CAPTURED
STATE+DIRECTIVE strings.

All tests mock validate_address_bounded + record_outcome — no live Google API
calls, no live Supabase RPCs.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tools.capture_lead import create_capture_lead_tool


# ── Test scaffolding ───────────────────────────────────────────────────────


def _make_supabase_mock() -> MagicMock:
    sb = MagicMock()
    # tenants.select().eq().single().execute() (currently used only in old code path,
    # not in the new D-E2 path — but keep for backward compat in case the assertion
    # touches it).
    tenant_chain = sb.table.return_value.select.return_value.eq.return_value
    tenant_chain.single.return_value.execute.return_value = MagicMock(
        data={"business_name": "Test Co"}
    )
    # calls.update().eq().is_().execute()
    update_chain = sb.table.return_value.update.return_value.eq.return_value
    update_chain.is_.return_value.execute.return_value = MagicMock(data=[])
    return sb


def _make_deps(supabase: MagicMock) -> dict:
    return {
        "supabase": supabase,
        "tenant_id": "tenant-1",
        "call_id": "call-1",
        "call_uuid": "call-uuid-1",
        "country": "US",
        "from_number": "+15551234567",
        "to_number": "+15557654321",
        "start_timestamp": 0,
    }


def _make_validation_result(verdict: str, formatted: str | None = None) -> dict:
    return {
        "verdict": verdict,
        "formatted_address": formatted,
        "place_id": "ChIJ-place-1" if formatted else None,
        "latitude": 37.4224 if formatted else None,
        "longitude": -122.0840 if formatted else None,
        "address_components": {
            "street_number": "1600",
            "route": "Amphitheatre Pkwy",
            "subpremise": None,
            "locality": "Mountain View",
            "admin_area_level_1": "CA",
            "admin_area_level_2": None,
            "postal_code": "94043",
            "country": "United States",
            "country_code": "US",
        } if formatted else {},
        "latency_ms": 120,
        "raw_status": 200,
    }


@pytest.fixture
def patched_handler():
    """Patches validate_address_bounded + record_outcome (both async) inside the
    capture_lead module."""
    with patch("src.tools.capture_lead.validate_address_bounded", new_callable=AsyncMock) as mock_validate, \
         patch("src.tools.capture_lead.record_outcome", new_callable=AsyncMock) as mock_record:
        yield {"validate": mock_validate, "record": mock_record}


# Common kwargs for invoking the @function_tool wrapped handler. The decorator
# uses the function signature (caller_name, phone, street_name, ...) — the
# inner function takes them as positional/kwarg args, NOT a dict raw_arguments.
def _kwargs(**overrides) -> dict:
    base = dict(
        caller_name="Alice",
        phone="",
        street_name="1600 Amphitheatre Pkwy",
        unit_number="",
        postal_code="94043",
        job_type="leak_repair",
        notes="",
    )
    base.update(overrides)
    return base


# ── D-D3' overwrite + D-B4 symmetry ────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_lead_confirmed_overwrites_service_address(patched_handler):
    sb = _make_supabase_mock()
    deps = _make_deps(sb)
    tool = create_capture_lead_tool(deps)

    patched_handler["validate"].return_value = _make_validation_result(
        "confirmed", formatted="1600 Amphitheatre Pkwy, Mountain View, CA 94043, USA"
    )
    patched_handler["record"].return_value = {"customer_id": "cust-1", "inquiry_id": "inq-1"}

    ctx = MagicMock()
    await tool.__wrapped__(ctx, **_kwargs())

    assert patched_handler["record"].await_count == 1
    call_kwargs = patched_handler["record"].await_args.kwargs
    assert call_kwargs["service_address"] == "1600 Amphitheatre Pkwy, Mountain View, CA 94043, USA"
    assert call_kwargs["address_validation_verdict"] == "confirmed"
    assert call_kwargs["formatted_address"] == "1600 Amphitheatre Pkwy, Mountain View, CA 94043, USA"


@pytest.mark.asyncio
async def test_capture_lead_unconfirmed_keeps_agent_joined(patched_handler):
    sb = _make_supabase_mock()
    deps = _make_deps(sb)
    tool = create_capture_lead_tool(deps)

    patched_handler["validate"].return_value = _make_validation_result(
        "unconfirmed", formatted=None
    )
    patched_handler["record"].return_value = {"customer_id": "cust-1", "inquiry_id": "inq-1"}

    ctx = MagicMock()
    await tool.__wrapped__(ctx, **_kwargs())

    call_kwargs = patched_handler["record"].await_args.kwargs
    assert call_kwargs["service_address"] == "1600 Amphitheatre Pkwy, 94043"
    assert call_kwargs["address_validation_verdict"] == "unconfirmed"


# ── D-E2 return-shape tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_lead_confirmed_return_shape(patched_handler):
    sb = _make_supabase_mock()
    deps = _make_deps(sb)
    tool = create_capture_lead_tool(deps)

    patched_handler["validate"].return_value = _make_validation_result(
        "confirmed", formatted="1600 Amphitheatre Pkwy, Mountain View, CA 94043, USA"
    )
    patched_handler["record"].return_value = {"customer_id": "cust-1", "inquiry_id": "inq-1"}

    ctx = MagicMock()
    result = await tool.__wrapped__(ctx, **_kwargs())

    assert result.startswith("LEAD CAPTURED [verdict=validated]:")
    assert "1600 Amphitheatre Pkwy, Mountain View, CA 94043, USA" in result
    assert "ask if anything else is needed" in result


@pytest.mark.asyncio
async def test_capture_lead_corrections_return_shape(patched_handler):
    sb = _make_supabase_mock()
    deps = _make_deps(sb)
    tool = create_capture_lead_tool(deps)

    patched_handler["validate"].return_value = _make_validation_result(
        "confirmed_with_changes",
        formatted="1600 Amphitheatre Parkway, Mountain View, CA 94043, USA",
    )
    patched_handler["record"].return_value = {"customer_id": "cust-1", "inquiry_id": "inq-1"}

    ctx = MagicMock()
    result = await tool.__wrapped__(ctx, **_kwargs())

    assert result.startswith("LEAD CAPTURED [verdict=validated_with_corrections]:")
    assert "1600 Amphitheatre Parkway, Mountain View, CA 94043, USA" in result
    assert "explicitly invite caller confirmation" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["unconfirmed", "error", "skipped", "unsupported_region"])
async def test_capture_lead_unvalidated_return_shape_for_each_other_verdict(patched_handler, verdict):
    sb = _make_supabase_mock()
    deps = _make_deps(sb)
    tool = create_capture_lead_tool(deps)

    patched_handler["validate"].return_value = _make_validation_result(verdict, formatted=None)
    patched_handler["record"].return_value = {"customer_id": "cust-1", "inquiry_id": "inq-1"}

    ctx = MagicMock()
    result = await tool.__wrapped__(ctx, **_kwargs())

    assert result.startswith("LEAD CAPTURED [verdict=unvalidated]:")
    assert "relay address as caller spoke it" in result
    assert "do NOT claim" in result
