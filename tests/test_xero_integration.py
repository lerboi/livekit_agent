"""Tests for src.integrations.xero (Phase 55)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.integrations import xero as xero_mod


@pytest.mark.asyncio
async def test_returns_none_for_invalid_phone():
    result = await xero_mod.fetch_xero_customer_by_phone("tenant-1", "not-e164")
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_no_credentials():
    with patch.object(xero_mod, "_load_credentials", AsyncMock(return_value=None)):
        result = await xero_mod.fetch_xero_customer_by_phone("tenant-1", "+15551234567")
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_on_no_match():
    cred = {
        "id": "cred-1",
        "tenant_id": "tenant-1",
        "xero_tenant_id": "org-1",
        "access_token": "tok",
        "refresh_token": "rt",
        "expiry_date": "2099-01-01T00:00:00+00:00",
    }
    with patch.object(xero_mod, "_load_credentials", AsyncMock(return_value=cred)), \
         patch.object(xero_mod, "_get_contacts_by_phone", AsyncMock(return_value=None)):
        result = await xero_mod.fetch_xero_customer_by_phone("tenant-1", "+15551234567")
    assert result is None


@pytest.mark.asyncio
async def test_returns_full_shape_on_match():
    cred = {
        "id": "cred-1",
        "tenant_id": "tenant-1",
        "xero_tenant_id": "org-1",
        "access_token": "tok",
        "refresh_token": "rt",
        "expiry_date": "2099-01-01T00:00:00+00:00",
    }
    contact = {
        "ContactID": "c1",
        "Name": "John Smith",
        "Phones": [{"PhoneNumber": "+15551234567"}],
    }
    recent = [
        {
            "InvoiceNumber": "INV-1",
            "Date": "2026-04-10",
            "Total": 500,
            "AmountDue": 500,
            "Status": "AUTHORISED",
            "Reference": "Repair",
            "FullyPaidOnDate": None,
        },
        {
            "InvoiceNumber": "INV-2",
            "Date": "2026-04-01",
            "Total": 250,
            "AmountDue": 0,
            "Status": "PAID",
            "Reference": "Service",
            "FullyPaidOnDate": "2026-04-05",
        },
    ]
    with patch.object(xero_mod, "_load_credentials", AsyncMock(return_value=cred)), \
         patch.object(xero_mod, "_get_contacts_by_phone", AsyncMock(return_value=contact)), \
         patch.object(xero_mod, "_get_outstanding_balance", AsyncMock(return_value=500.0)), \
         patch.object(xero_mod, "_get_recent_invoices", AsyncMock(return_value=recent)), \
         patch.object(xero_mod, "_touch_last_context_fetch_at", AsyncMock()):
        result = await xero_mod.fetch_xero_customer_by_phone("tenant-1", "+15551234567")

    assert result is not None
    assert result["contact"]["contact_id"] == "c1"
    assert result["outstanding_balance"] == 500.0
    assert len(result["last_invoices"]) == 2
    assert result["last_invoices"][0]["invoice_number"] == "INV-1"
    assert result["last_payment_date"] == "2026-04-05"


@pytest.mark.asyncio
async def test_refresh_persists_new_tokens_on_expired():
    expired_cred = {
        "id": "cred-1",
        "tenant_id": "tenant-1",
        "xero_tenant_id": "org-1",
        "access_token": "old",
        "refresh_token": "old-rt",
        "expiry_date": "2000-01-01T00:00:00+00:00",
    }
    refresh_resp = MagicMock()
    refresh_resp.status_code = 200
    refresh_resp.json.return_value = {
        "access_token": "new",
        "refresh_token": "new-rt",
        "expires_in": 1800,
    }

    persist_mock = AsyncMock()
    with patch.object(xero_mod, "_load_credentials", AsyncMock(return_value=expired_cred)), \
         patch.object(xero_mod, "_persist_refreshed_tokens", persist_mock), \
         patch.object(xero_mod, "_get_contacts_by_phone", AsyncMock(return_value=None)), \
         patch.dict("os.environ", {"XERO_CLIENT_ID": "id", "XERO_CLIENT_SECRET": "secret"}), \
         patch("httpx.AsyncClient.post", AsyncMock(return_value=refresh_resp)):
        await xero_mod.fetch_xero_customer_by_phone("tenant-1", "+15551234567")

    persist_mock.assert_awaited_once()
    args = persist_mock.call_args.args
    assert args[0] == "cred-1"
    assert args[1] == "new"
    assert args[2] == "new-rt"


@pytest.mark.asyncio
async def test_refresh_failure_persists_error_state_and_returns_none():
    expired_cred = {
        "id": "cred-1",
        "tenant_id": "tenant-1",
        "xero_tenant_id": "org-1",
        "access_token": "old",
        "refresh_token": "revoked-rt",
        "expiry_date": "2000-01-01T00:00:00+00:00",
    }
    bad_resp = MagicMock()
    bad_resp.status_code = 400
    bad_resp.json.return_value = {"error": "invalid_grant"}

    persist_failure_mock = AsyncMock()
    with patch.object(xero_mod, "_load_credentials", AsyncMock(return_value=expired_cred)), \
         patch.object(xero_mod, "_persist_refresh_failure", persist_failure_mock), \
         patch.dict("os.environ", {"XERO_CLIENT_ID": "id", "XERO_CLIENT_SECRET": "secret"}), \
         patch("httpx.AsyncClient.post", AsyncMock(return_value=bad_resp)):
        result = await xero_mod.fetch_xero_customer_by_phone("tenant-1", "+15551234567")

    assert result is None
    persist_failure_mock.assert_awaited_once_with("cred-1")
