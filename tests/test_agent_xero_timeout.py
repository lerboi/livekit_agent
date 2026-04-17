"""Tests for the 800ms-bounded xero context fetch (Phase 55 Plan 06).

Tests the `fetch_xero_context_bounded` helper directly rather than the nested
`_run_db_queries` inside agent.py (which closes over the job context and is
not independently callable).
"""
import asyncio
from unittest.mock import patch

import pytest

from src.integrations import xero as xero_mod


@pytest.mark.asyncio
async def test_xero_timeout_returns_none():
    async def slow_fetch(*_args, **_kwargs):
        await asyncio.sleep(2.0)  # exceeds 800ms budget
        return {"contact": "should-never-return"}

    with patch.object(xero_mod, "fetch_xero_customer_by_phone", slow_fetch):
        result = await xero_mod.fetch_xero_context_bounded(
            "tenant-1", "+15551234567", timeout_seconds=0.1
        )

    assert result is None


@pytest.mark.asyncio
async def test_xero_success_returns_shape():
    async def fast_fetch(*_args, **_kwargs):
        return {
            "contact": {"name": "John"},
            "outstanding_balance": 100.0,
            "last_invoices": [],
            "last_payment_date": None,
        }

    with patch.object(xero_mod, "fetch_xero_customer_by_phone", fast_fetch):
        result = await xero_mod.fetch_xero_context_bounded(
            "tenant-1", "+15551234567", timeout_seconds=0.8
        )

    assert result is not None
    assert result["contact"]["name"] == "John"


@pytest.mark.asyncio
async def test_xero_exception_returns_none_and_captures_sentry():
    async def failing_fetch(*_args, **_kwargs):
        raise RuntimeError("xero down")

    captured = []

    def _fake_capture(exc, tags=None):
        captured.append({"exc": exc, "tags": tags})

    with patch.object(xero_mod, "fetch_xero_customer_by_phone", failing_fetch), \
         patch("sentry_sdk.capture_exception", side_effect=_fake_capture):
        result = await xero_mod.fetch_xero_context_bounded(
            "tenant-1", "+15551234567", timeout_seconds=0.8
        )

    assert result is None
    assert len(captured) == 1
    assert isinstance(captured[0]["exc"], RuntimeError)
    assert captured[0]["tags"]["phase"] == "55"
    assert captured[0]["tags"]["component"] == "xero_context_fetch"
    # Phone is hashed, not raw
    assert "+15551234567" not in str(captured[0]["tags"])
