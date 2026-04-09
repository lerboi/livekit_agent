"""Unit tests for check_outbound_cap() — Plan 39-04.

Tests mock get_supabase_admin to return controlled row lists. The function
sums outbound_dial_duration_sec from the returned rows and compares against
the country limit (US/CA: 300000s, SG: 150000s, unknown: 300000s fallback).
"""
from unittest.mock import MagicMock

import pytest

from src.webhook.caps import check_outbound_cap


def _make_mock_supabase(rows: list[dict]) -> MagicMock:
    """Build a MagicMock that mimics the supabase-py chain:
    supabase.table(...).select(...).eq(...).gte(...).execute() -> response.data = rows
    """
    response = MagicMock()
    response.data = rows
    chain = MagicMock()
    chain.execute.return_value = response
    chain.gte.return_value = chain
    chain.eq.return_value = chain
    chain.select.return_value = chain
    supabase = MagicMock()
    supabase.table.return_value = chain
    return supabase


@pytest.mark.asyncio
async def test_under_cap_us_returns_true(monkeypatch):
    """US tenant with 100000 seconds used (100000 < 300000) -> True."""
    rows = [{"outbound_dial_duration_sec": 100000}]
    monkeypatch.setattr(
        "src.supabase_client.get_supabase_admin",
        lambda: _make_mock_supabase(rows),
    )
    result = await check_outbound_cap("tenant-123", "US")
    assert result is True


@pytest.mark.asyncio
async def test_at_cap_us_returns_false(monkeypatch):
    """US tenant with exactly 300000 seconds used -> False (at cap)."""
    rows = [{"outbound_dial_duration_sec": 300000}]
    monkeypatch.setattr(
        "src.supabase_client.get_supabase_admin",
        lambda: _make_mock_supabase(rows),
    )
    result = await check_outbound_cap("tenant-123", "US")
    assert result is False


@pytest.mark.asyncio
async def test_at_cap_ca_returns_false(monkeypatch):
    """CA tenant at exactly the 5000-minute cap -> False. Validates CA is in _LIMITS_SEC and shares the US limit."""
    rows = [{"outbound_dial_duration_sec": 300000}]
    monkeypatch.setattr(
        "src.supabase_client.get_supabase_admin",
        lambda: _make_mock_supabase(rows),
    )
    result = await check_outbound_cap("tenant-ca", "CA")
    assert result is False


@pytest.mark.asyncio
async def test_over_cap_us_returns_false(monkeypatch):
    """US tenant with 400000 seconds used (split across two rows) -> False."""
    rows = [
        {"outbound_dial_duration_sec": 200000},
        {"outbound_dial_duration_sec": 200000},
    ]
    monkeypatch.setattr(
        "src.supabase_client.get_supabase_admin",
        lambda: _make_mock_supabase(rows),
    )
    result = await check_outbound_cap("tenant-123", "US")
    assert result is False


@pytest.mark.asyncio
async def test_under_cap_sg_returns_true(monkeypatch):
    """SG tenant with 100000 seconds used (100000 < 150000) -> True."""
    rows = [{"outbound_dial_duration_sec": 100000}]
    monkeypatch.setattr(
        "src.supabase_client.get_supabase_admin",
        lambda: _make_mock_supabase(rows),
    )
    result = await check_outbound_cap("tenant-456", "SG")
    assert result is True


@pytest.mark.asyncio
async def test_at_cap_sg_returns_false(monkeypatch):
    """SG tenant with exactly 150000 seconds used -> False."""
    rows = [{"outbound_dial_duration_sec": 150000}]
    monkeypatch.setattr(
        "src.supabase_client.get_supabase_admin",
        lambda: _make_mock_supabase(rows),
    )
    result = await check_outbound_cap("tenant-456", "SG")
    assert result is False


@pytest.mark.asyncio
async def test_unknown_country_falls_back_to_us_limit(monkeypatch):
    """country='XX' (unknown) uses US limit (300000 seconds) - 200000 < 300000 -> True."""
    rows = [{"outbound_dial_duration_sec": 200000}]
    monkeypatch.setattr(
        "src.supabase_client.get_supabase_admin",
        lambda: _make_mock_supabase(rows),
    )
    result = await check_outbound_cap("tenant-789", "XX")
    assert result is True


@pytest.mark.asyncio
async def test_zero_seconds_used_returns_true(monkeypatch):
    """Fresh tenant with no rows returned -> 0 seconds used < any cap -> True."""
    monkeypatch.setattr(
        "src.supabase_client.get_supabase_admin",
        lambda: _make_mock_supabase([]),
    )
    result = await check_outbound_cap("tenant-new", "US")
    assert result is True
