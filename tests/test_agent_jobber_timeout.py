"""Tests for the jobber+xero concurrent context fetch (Phase 56 Plan 06).

Tests `fetch_merged_customer_context_bounded` directly (the helper in
src/lib/customer_context.py that races both providers in parallel) rather
than the entrypoint's `_run_db_queries` (which closes over JobContext).

Mirrors the pattern of test_agent_xero_timeout.py.
"""
import asyncio
import time
from unittest.mock import patch

import pytest

from src.lib import customer_context as cc_mod


@pytest.mark.asyncio
async def test_T1_jobber_timeout_yields_xero_only_context():
    """Jobber takes 2s; Xero returns fast; Jobber half silent-skipped."""
    async def slow_jobber(*a, **kw):
        await asyncio.sleep(2.0)
        return {"client": {"id": "j1", "name": "J"}, "recentJobs": [],
                "outstandingInvoices": [], "outstandingBalance": 0,
                "lastVisitDate": None}

    async def fast_xero(*a, **kw):
        return {"contact": {"contact_id": "x1", "name": "X"},
                "outstanding_balance": 50.0, "last_invoices": [],
                "last_payment_date": "2026-04-01"}

    captured = []

    def _fake_capture(msg, tags=None, **kw):
        captured.append({"msg": msg, "tags": tags or kw.get("tags")})

    with patch.object(cc_mod, "fetch_jobber_customer_by_phone", side_effect=slow_jobber), \
         patch.object(cc_mod, "fetch_xero_customer_by_phone", side_effect=fast_xero), \
         patch("sentry_sdk.capture_message", side_effect=_fake_capture):
        result = await cc_mod.fetch_merged_customer_context_bounded(
            "t1", "+15551234567", timeout_seconds=0.1
        )

    assert result is not None
    # Xero-only populated
    assert "lastPaymentDate" in result
    assert "recentJobs" not in result  # Jobber timed out
    # Sentry capture tagged with provider=jobber
    jobber_captures = [c for c in captured if (c["tags"] or {}).get("provider") == "jobber"]
    assert len(jobber_captures) >= 1
    tags = jobber_captures[0]["tags"]
    assert tags.get("tenant_id") == "t1"
    assert "phone_hash" in tags
    # Raw phone never in tags
    assert "+15551234567" not in str(tags)


@pytest.mark.asyncio
async def test_T2_jobber_exception_still_yields_xero_context():
    async def raising_jobber(*a, **kw):
        raise RuntimeError("api down")

    async def fast_xero(*a, **kw):
        return {"contact": {"contact_id": "x1", "name": "X"},
                "outstanding_balance": 0, "last_invoices": [],
                "last_payment_date": None}

    captured_exc = []

    def _fake_cap_exc(exc, tags=None, **kw):
        captured_exc.append({"exc": exc, "tags": tags or kw.get("tags")})

    with patch.object(cc_mod, "fetch_jobber_customer_by_phone", side_effect=raising_jobber), \
         patch.object(cc_mod, "fetch_xero_customer_by_phone", side_effect=fast_xero), \
         patch("sentry_sdk.capture_exception", side_effect=_fake_cap_exc):
        result = await cc_mod.fetch_merged_customer_context_bounded(
            "t1", "+15551234567", timeout_seconds=0.8
        )

    assert result is not None
    # At least one exception captured with provider=jobber
    jobber_excs = [c for c in captured_exc if (c["tags"] or {}).get("provider") == "jobber"]
    assert len(jobber_excs) >= 1


@pytest.mark.asyncio
async def test_T3_both_providers_miss_yields_none_context():
    async def miss(*a, **kw):
        return None

    with patch.object(cc_mod, "fetch_jobber_customer_by_phone", side_effect=miss), \
         patch.object(cc_mod, "fetch_xero_customer_by_phone", side_effect=miss):
        result = await cc_mod.fetch_merged_customer_context_bounded(
            "t1", "+15551234567", timeout_seconds=0.8
        )
    assert result is None


@pytest.mark.asyncio
async def test_T4_jobber_and_xero_run_concurrently_not_serially():
    """Verify both tasks run in parallel — not sequentially."""
    started_at = []

    async def slow_600ms_jobber(*a, **kw):
        started_at.append(("jobber", time.monotonic()))
        await asyncio.sleep(0.6)
        return None

    async def slow_600ms_xero(*a, **kw):
        started_at.append(("xero", time.monotonic()))
        await asyncio.sleep(0.6)
        return None

    t0 = time.monotonic()
    with patch.object(cc_mod, "fetch_jobber_customer_by_phone", side_effect=slow_600ms_jobber), \
         patch.object(cc_mod, "fetch_xero_customer_by_phone", side_effect=slow_600ms_xero):
        await cc_mod.fetch_merged_customer_context_bounded(
            "t1", "+15551234567", timeout_seconds=1.5
        )
    elapsed = time.monotonic() - t0
    # Concurrent ≈0.6s; serial would be ≈1.2s
    assert elapsed < 1.0, f"expected concurrent <1s, got {elapsed:.2f}s"
    assert len(started_at) == 2
    jobber_start = next(t for n, t in started_at if n == "jobber")
    xero_start = next(t for n, t in started_at if n == "xero")
    assert abs(jobber_start - xero_start) < 0.05
