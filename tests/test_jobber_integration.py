"""Phase 56 Plan 05 — pytest unit tests for Jobber integration adapter.

Covers:
- no-credentials path → None
- free-form phone match via phonenumbers normalization
- outstanding-balance filter (DRAFT/PAID/VOIDED excluded)
- refresh-token rotation write-back (new refresh_token MUST be persisted)
- never-raises on network error
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.integrations import jobber as jobber_mod


@pytest.mark.asyncio
async def test_returns_none_for_invalid_inputs():
    assert await jobber_mod.fetch_jobber_customer_by_phone("", "+15551234567") is None
    assert await jobber_mod.fetch_jobber_customer_by_phone("tenant-1", "") is None


@pytest.mark.asyncio
async def test_returns_none_when_no_credentials():
    with patch.object(jobber_mod, "_load_credentials", AsyncMock(return_value=None)):
        result = await jobber_mod.fetch_jobber_customer_by_phone("tenant-1", "+15551234567")
    assert result is None


@pytest.mark.asyncio
async def test_matches_freeform_phone():
    """Jobber stores '(555) 123-4567' — must match +15551234567 via normalization."""
    cred = {
        "id": "cred-1",
        "tenant_id": "tenant-1",
        "provider": "jobber",
        "access_token": "tok",
        "refresh_token": "rt",
        "expiry_date": "2099-01-01T00:00:00+00:00",
    }
    gql_response = MagicMock(status_code=200)
    gql_response.json.return_value = {
        "data": {
            "clients": {
                "nodes": [
                    {
                        "id": "jc1",
                        "name": "John Smith",
                        "emails": [{"address": "john@example.com"}],
                        "phones": [{"number": "(555) 123-4567"}],
                        "jobs": {"nodes": []},
                        "invoices": {"nodes": []},
                        "visits": {"nodes": []},
                    }
                ]
            }
        }
    }
    with patch.object(jobber_mod, "_load_credentials", AsyncMock(return_value=cred)), \
         patch.object(jobber_mod, "_touch_last_context_fetch_at", AsyncMock()), \
         patch("httpx.AsyncClient.post", AsyncMock(return_value=gql_response)):
        result = await jobber_mod.fetch_jobber_customer_by_phone("tenant-1", "+15551234567")

    assert result is not None
    assert result["client"]["id"] == "jc1"
    assert result["client"]["name"] == "John Smith"
    assert result["client"]["email"] == "john@example.com"
    assert result["recentJobs"] == []
    assert result["outstandingBalance"] == 0.0
    assert result["lastVisitDate"] is None


@pytest.mark.asyncio
async def test_outstanding_balance_excludes_paid_draft_voided():
    cred = {
        "id": "cred-1",
        "tenant_id": "tenant-1",
        "provider": "jobber",
        "access_token": "tok",
        "refresh_token": "rt",
        "expiry_date": "2099-01-01T00:00:00+00:00",
    }
    gql_response = MagicMock(status_code=200)
    gql_response.json.return_value = {
        "data": {
            "clients": {
                "nodes": [
                    {
                        "id": "jc1",
                        "name": "X",
                        "emails": [],
                        "phones": [{"number": "+15551234567"}],
                        "jobs": {"nodes": []},
                        "invoices": {"nodes": [
                            {"invoiceNumber": "A", "issuedDate": "2026-04-01", "amount": 100, "amountOutstanding": 100, "invoiceStatus": "AWAITING_PAYMENT"},
                            {"invoiceNumber": "B", "issuedDate": "2026-04-01", "amount": 200, "amountOutstanding": 200, "invoiceStatus": "PAST_DUE"},
                            {"invoiceNumber": "C", "issuedDate": "2026-04-01", "amount": 50,  "amountOutstanding": 50,  "invoiceStatus": "DRAFT"},
                            {"invoiceNumber": "D", "issuedDate": "2026-04-01", "amount": 999, "amountOutstanding": 0,   "invoiceStatus": "PAID"},
                            {"invoiceNumber": "E", "issuedDate": "2026-04-01", "amount": 10,  "amountOutstanding": 10,  "invoiceStatus": "VOIDED"},
                        ]},
                        "visits": {"nodes": []},
                    }
                ]
            }
        }
    }
    with patch.object(jobber_mod, "_load_credentials", AsyncMock(return_value=cred)), \
         patch.object(jobber_mod, "_touch_last_context_fetch_at", AsyncMock()), \
         patch("httpx.AsyncClient.post", AsyncMock(return_value=gql_response)):
        result = await jobber_mod.fetch_jobber_customer_by_phone("tenant-1", "+15551234567")

    assert result is not None
    assert result["outstandingBalance"] == 300.0
    assert len(result["outstandingInvoices"]) == 2
    numbers = {inv["invoiceNumber"] for inv in result["outstandingInvoices"]}
    assert numbers == {"A", "B"}


@pytest.mark.asyncio
async def test_refresh_rotation_persists_new_refresh_token():
    """On 401, refresh + retry + the NEW refresh_token MUST be persisted."""
    # JWT w/ exp far in the future (so expiry parse works)
    import base64
    import json as _json
    jwt_payload = base64.urlsafe_b64encode(_json.dumps({"exp": 9999999999}).encode()).rstrip(b"=").decode()
    jwt = f"h.{jwt_payload}.s"

    cred = {
        "id": "cred-1",
        "tenant_id": "tenant-1",
        "provider": "jobber",
        "access_token": "old-tok",
        "refresh_token": "rt-old",
        "expiry_date": "2099-01-01T00:00:00+00:00",  # not expired — force reactive 401 path
    }

    # First GraphQL call → 401; refresh POST → 200 w/ new refresh_token; second GraphQL → 200 empty
    first_gql = MagicMock(status_code=401)
    refresh_resp = MagicMock(status_code=200)
    refresh_resp.json.return_value = {"access_token": jwt, "refresh_token": "rt-NEW-rotated"}
    second_gql = MagicMock(status_code=200)
    second_gql.json.return_value = {"data": {"clients": {"nodes": []}}}

    call_seq = [first_gql, refresh_resp, second_gql]

    async def _post(*args, **kwargs):
        return call_seq.pop(0)

    persist_mock = AsyncMock()
    with patch.object(jobber_mod, "_load_credentials", AsyncMock(return_value=cred)), \
         patch.object(jobber_mod, "_persist_refreshed_tokens", persist_mock), \
         patch.dict("os.environ", {"JOBBER_CLIENT_ID": "id", "JOBBER_CLIENT_SECRET": "secret"}), \
         patch("httpx.AsyncClient.post", _post):
        await jobber_mod.fetch_jobber_customer_by_phone("tenant-1", "+15551234567")

    # Assert the write-back received the NEW refresh_token (rotation)
    persist_mock.assert_awaited()
    args = persist_mock.call_args.args
    # signature: (cred_id, access_token, refresh_token, expiry_iso)
    assert args[0] == "cred-1"
    assert args[1] == jwt
    assert args[2] == "rt-NEW-rotated"


@pytest.mark.asyncio
async def test_never_raises_on_network_error():
    cred = {
        "id": "cred-1",
        "tenant_id": "tenant-1",
        "provider": "jobber",
        "access_token": "tok",
        "refresh_token": "rt",
        "expiry_date": "2099-01-01T00:00:00+00:00",
    }
    with patch.object(jobber_mod, "_load_credentials", AsyncMock(return_value=cred)), \
         patch("httpx.AsyncClient.post", AsyncMock(side_effect=Exception("connection reset"))):
        result = await jobber_mod.fetch_jobber_customer_by_phone("tenant-1", "+15551234567")
    assert result is None
