"""Phase 61 Plan 03 — book_appointment integration tests for D-D3' + D-E2 contracts.

Locks the validate-then-book flow: validate_address_bounded runs BEFORE
atomic_book_slot, formatted_address overwrites service_address ONLY on
confirmed/confirmed_with_changes verdicts, and the success-path tool return
emits one of three D-E2 STATE+DIRECTIVE strings keyed by verdict.

All tests mock at the validate_address_bounded + atomic_book_slot boundaries —
no live Google API calls, no live Supabase RPCs.
"""
from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tools.book_appointment import create_book_appointment_tool


# ── Test scaffolding ───────────────────────────────────────────────────────


def _make_supabase_mock() -> MagicMock:
    """Mock supabase client supporting the tenants .select().single().execute()
    chain and the calls.update().eq().is_().execute() / .eq().execute() chains
    that book_appointment's success path touches.
    """
    sb = MagicMock()

    # tenants.select().eq().single().execute() → tenant config
    tenant_chain = sb.table.return_value.select.return_value.eq.return_value
    tenant_chain.single.return_value.execute.return_value = MagicMock(
        data={
            "tenant_timezone": "America/Los_Angeles",
            "working_hours": {},
            "slot_duration_mins": 60,
            "business_name": "Test Co",
            "default_locale": "en",
        }
    )

    # calls.update(...).eq("call_id", ...).is_(...).execute() and .eq().execute()
    update_chain = sb.table.return_value.update.return_value.eq.return_value
    update_chain.is_.return_value.execute.return_value = MagicMock(data=[])
    update_chain.execute.return_value = MagicMock(data=[{"call_id": "c1"}])
    return sb


def _make_deps(supabase: MagicMock) -> dict:
    """Build a deps dict with the keys the book_appointment handler reads."""
    # Stash a known slot_token so the handler resolves slot_start/slot_end
    # without needing to invent one.
    slot_token = "tok-1"
    slot_start = "2026-05-04T20:00:00+00:00"
    slot_end = "2026-05-04T21:00:00+00:00"
    return {
        "supabase": supabase,
        "tenant_id": "tenant-1",
        "call_id": "call-1",
        "call_uuid": "call-uuid-1",
        "country": "US",
        "from_number": "+15551234567",
        "to_number": "+15557654321",
        "_slot_tokens": {
            slot_token: {
                "slot_start_utc": slot_start,
                "slot_end_utc": slot_end,
                "created_at": time.time(),
            }
        },
        "_last_offered_token": slot_token,
    }


def _raw_args(**overrides) -> dict:
    base = {
        "slot_token": "tok-1",
        "street_name": "1600 Amphitheatre Pkwy",
        "postal_code": "94043",
        "caller_name": "Alice",
        "unit_number": "",
        "urgency": "routine",
    }
    base.update(overrides)
    return base


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
    """Patches validate_address_with_region_fallback + atomic_book_slot (both
    async) inside the book_appointment module. Yields {'validate': mock,
    'atomic': mock} where each is an AsyncMock — set .return_value on each per
    test to control behavior. The region-fallback wrapper returns a
    (result, region_used) tuple; the side_effect below wraps the bare result
    dict each test assigns so the tests stay tuple-agnostic.
    """
    with patch("src.tools.book_appointment.validate_address_with_region_fallback", new_callable=AsyncMock) as mock_validate, \
         patch("src.tools.book_appointment.atomic_book_slot", new_callable=AsyncMock) as mock_atomic, \
         patch("src.tools.book_appointment.push_booking_to_calendar"), \
         patch("src.tools.book_appointment.send_caller_sms"), \
         patch("src.tools.book_appointment.send_caller_recovery_sms"):
        mock_validate.side_effect = lambda *a, **k: (mock_validate.return_value, "US")
        yield {"validate": mock_validate, "atomic": mock_atomic}


# ── D-D3' overwrite tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_confirmed_overwrites_service_address(patched_handler):
    sb = _make_supabase_mock()
    deps = _make_deps(sb)
    tool = create_book_appointment_tool(deps)

    patched_handler["validate"].return_value = _make_validation_result(
        "confirmed", formatted="1600 Amphitheatre Pkwy, Mountain View, CA 94043, USA"
    )
    patched_handler["atomic"].return_value = {"success": True, "appointment_id": "a1"}

    ctx = MagicMock()
    await tool.__wrapped__(_raw_args(), ctx)

    # atomic_book_slot was called with address=normalized formatted_address
    assert patched_handler["atomic"].await_count == 1
    call_kwargs = patched_handler["atomic"].await_args.kwargs
    assert call_kwargs["address"] == "1600 Amphitheatre Pkwy, Mountain View, CA 94043, USA"
    assert call_kwargs["address_validation_verdict"] == "confirmed"
    assert call_kwargs["formatted_address"] == "1600 Amphitheatre Pkwy, Mountain View, CA 94043, USA"


@pytest.mark.asyncio
async def test_unconfirmed_keeps_agent_joined(patched_handler):
    sb = _make_supabase_mock()
    deps = _make_deps(sb)
    tool = create_book_appointment_tool(deps)

    patched_handler["validate"].return_value = _make_validation_result(
        "unconfirmed", formatted=None
    )
    patched_handler["atomic"].return_value = {"success": True, "appointment_id": "a1"}

    ctx = MagicMock()
    await tool.__wrapped__(_raw_args(), ctx)

    call_kwargs = patched_handler["atomic"].await_args.kwargs
    # Address is the agent-joined "street, postal" string (no unit supplied)
    assert call_kwargs["address"] == "1600 Amphitheatre Pkwy, 94043"
    assert call_kwargs["address_validation_verdict"] == "unconfirmed"


@pytest.mark.asyncio
async def test_error_keeps_agent_joined_and_proceeds(patched_handler):
    """D-C1: booking never blocks on Google. Verdict=error → atomic_book_slot still runs."""
    sb = _make_supabase_mock()
    deps = _make_deps(sb)
    tool = create_book_appointment_tool(deps)

    patched_handler["validate"].return_value = _make_validation_result(
        "error", formatted=None
    )
    patched_handler["atomic"].return_value = {"success": True, "appointment_id": "a1"}

    ctx = MagicMock()
    await tool.__wrapped__(_raw_args(), ctx)

    assert patched_handler["atomic"].await_count == 1
    call_kwargs = patched_handler["atomic"].await_args.kwargs
    assert call_kwargs["address"] == "1600 Amphitheatre Pkwy, 94043"
    assert call_kwargs["address_validation_verdict"] == "error"


@pytest.mark.asyncio
async def test_skipped_keeps_agent_joined(patched_handler):
    sb = _make_supabase_mock()
    deps = _make_deps(sb)
    tool = create_book_appointment_tool(deps)

    patched_handler["validate"].return_value = _make_validation_result(
        "skipped", formatted=None
    )
    patched_handler["atomic"].return_value = {"success": True, "appointment_id": "a1"}

    ctx = MagicMock()
    await tool.__wrapped__(_raw_args(), ctx)

    assert patched_handler["atomic"].await_count == 1
    call_kwargs = patched_handler["atomic"].await_args.kwargs
    assert call_kwargs["address"] == "1600 Amphitheatre Pkwy, 94043"
    assert call_kwargs["address_validation_verdict"] == "skipped"


# ── D-E2 return-shape tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_confirmed_return_shape(patched_handler):
    sb = _make_supabase_mock()
    deps = _make_deps(sb)
    tool = create_book_appointment_tool(deps)

    patched_handler["validate"].return_value = _make_validation_result(
        "confirmed", formatted="1600 Amphitheatre Pkwy, Mountain View, CA 94043, USA"
    )
    patched_handler["atomic"].return_value = {"success": True, "appointment_id": "a1"}

    ctx = MagicMock()
    result = await tool.__wrapped__(_raw_args(), ctx)

    assert result.startswith("BOOKED [verdict=validated]:")
    assert "1600 Amphitheatre Pkwy, Mountain View, CA 94043, USA" in result
    assert "ask if anything else is needed" in result


@pytest.mark.asyncio
async def test_corrections_return_shape(patched_handler):
    sb = _make_supabase_mock()
    deps = _make_deps(sb)
    tool = create_book_appointment_tool(deps)

    patched_handler["validate"].return_value = _make_validation_result(
        "confirmed_with_changes",
        formatted="1600 Amphitheatre Parkway, Mountain View, CA 94043, USA",
    )
    patched_handler["atomic"].return_value = {"success": True, "appointment_id": "a1"}

    ctx = MagicMock()
    result = await tool.__wrapped__(_raw_args(), ctx)

    assert result.startswith("BOOKED [verdict=validated_with_corrections]:")
    assert "1600 Amphitheatre Parkway, Mountain View, CA 94043, USA" in result
    assert "explicitly invite caller confirmation" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["unconfirmed", "error", "skipped", "unsupported_region"])
async def test_unvalidated_return_shape_for_each_other_verdict(patched_handler, verdict):
    sb = _make_supabase_mock()
    deps = _make_deps(sb)
    tool = create_book_appointment_tool(deps)

    patched_handler["validate"].return_value = _make_validation_result(verdict, formatted=None)
    patched_handler["atomic"].return_value = {"success": True, "appointment_id": "a1"}

    ctx = MagicMock()
    result = await tool.__wrapped__(_raw_args(), ctx)

    assert result.startswith("BOOKED [verdict=unvalidated]:")
    assert "relay address as caller spoke it" in result
    assert "do NOT claim" in result
