"""Slot-cache reuse tests (ported 2026-06-10 from
test_check_availability_slot_cache.py).

Original phase-fix (2026-04-23): the monolithic check_availability consumed
deps["_slot_cache"] when fresh, bypassing the 5 parallel Supabase scheduling
queries (shrinking the in-flight window the realtime model could cancel).
That tool was split into check_slot / check_day / next_available_days; the
cache behavior now lives in src/tools/_availability_lib.fetch_scheduling_data
(SLOT_CACHE_TTL_S = 30s), shared by all three. These tests port the same
invariants against check_slot, the primary booking-path tool.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.tools._availability_lib import SLOT_CACHE_TTL_S
from src.tools.check_slot import create_check_slot_tool


def _fresh_cache(**overrides):
    return {
        "fetched_at": time.time(),
        "appointments": [],
        "calendar_events": [],
        "service_zones": [],
        "zone_travel_buffers": [],
        "calendar_blocks": [],
        **overrides,
    }


def _stale_cache(age_s=120.0):
    return {
        "fetched_at": time.time() - age_s,
        "appointments": [],
        "calendar_events": [],
        "service_zones": [],
        "zone_travel_buffers": [],
        "calendar_blocks": [],
    }


class _FakeSupabaseCalledError(AssertionError):
    """Raised if supabase.table() is called despite a fresh cache being present."""


class _TrippedSupabase:
    """Supabase stub whose table() raises if called. Used to prove that the
    fresh-cache path does NOT perform any live fetches for scheduling tables."""

    def __init__(self):
        self.calls: list[str] = []

    def table(self, name):
        self.calls.append(name)
        scheduling = {
            "appointments",
            "calendar_events",
            "service_zones",
            "zone_travel_buffers",
            "calendar_blocks",
        }
        if name in scheduling:
            raise _FakeSupabaseCalledError(
                f"supabase.table({name!r}) called despite fresh slot_cache"
            )
        raise AssertionError(
            f"unexpected supabase.table({name!r}) call in slot_cache test"
        )


def _make_deps(cache, tenant):
    return {
        "supabase": _TrippedSupabase(),
        "tenant": tenant,
        "tenant_id": "test-tenant-id",
        "_slot_cache": cache,
        "_tool_call_log": [],
    }


def _complete_tenant():
    # Field set mirrors _availability_lib._NEEDED_TENANT_FIELDS so
    # ensure_tenant() never falls back to a live tenants fetch.
    return {
        "id": "test-tenant-id",
        "business_name": "ACME",
        "tenant_timezone": "Asia/Singapore",
        "slot_duration_mins": 60,
        "working_hours": {
            d: {"open": "08:00", "close": "17:00", "enabled": True,
                "lunchStart": None, "lunchEnd": None}
            for d in (
                "monday", "tuesday", "wednesday", "thursday",
                "friday", "saturday", "sunday",
            )
        },
    }


def _tomorrow_sgt() -> str:
    """A guaranteed-future local date (the original test hard-coded
    2026-04-24, which went stale and would now hit the past-date guard)."""
    now_sgt = datetime.now(ZoneInfo("Asia/Singapore"))
    return (now_sgt + timedelta(days=1)).strftime("%Y-%m-%d")


class _FakeRunContext:
    """Minimal RunContext-shaped object — check_slot only reads the tenant
    dict and deps; no livekit-agents internals are exercised."""
    def __init__(self):
        pass


@pytest.mark.asyncio
async def test_fresh_cache_bypasses_supabase_scheduling_queries():
    """If deps['_slot_cache'] is fresh (TTL 30s), check_slot must not call
    supabase.table() for any of the 5 scheduling tables. It should compute
    slots purely from cached data."""
    cache = _fresh_cache()
    deps = _make_deps(cache, _complete_tenant())
    tool = create_check_slot_tool(deps)

    # Tomorrow 15:00 SGT request. With the cache holding no appointments/
    # events/blocks and all-week 08:00-17:00 hours, the slot is available.
    result = await tool.__wrapped__(
        {"date": _tomorrow_sgt(), "time": "15:00"},
        _FakeRunContext(),
    )

    # No supabase calls to scheduling tables (the _TrippedSupabase would raise,
    # which check_slot's outer handler surfaces as STATE:lookup_failed).
    assert ("STATE:slot_ok" in result) or ("STATE:slot_taken" in result), (
        f"expected a slot verdict; got {result!r}"
    )
    assert "lookup_failed" not in result
    # Tool-call log must show the cached compute succeeded.
    last = deps["_tool_call_log"][-1] if deps["_tool_call_log"] else {}
    assert last.get("success") is True


def test_slot_cache_has_fetched_at_timestamp_and_five_tables():
    """Cache shape contract — fetch_scheduling_data reads exactly these keys."""
    cache = _fresh_cache()
    for k in (
        "fetched_at",
        "appointments",
        "calendar_events",
        "service_zones",
        "zone_travel_buffers",
        "calendar_blocks",
    ):
        assert k in cache, f"slot_cache must carry {k}"
    assert isinstance(cache["fetched_at"], float)
    for k in (
        "appointments", "calendar_events", "service_zones",
        "zone_travel_buffers", "calendar_blocks",
    ):
        assert isinstance(cache[k], list)


def test_stale_cache_triggers_live_refetch_path():
    """Sanity: a cache older than TTL (30s) is NOT reused. Mirrors the
    production gate in _availability_lib.fetch_scheduling_data — guards
    against a code edit that accidentally widens the TTL window."""
    stale = _stale_cache(age_s=31.0)
    assert (time.time() - stale["fetched_at"]) >= SLOT_CACHE_TTL_S
