"""Tests for the Xero contact→phone digits-only matcher (Phase 55 post-UAT fix)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.integrations import xero as xero_mod


def _cred():
    return {
        "id": "cred-1",
        "tenant_id": "tenant-1",
        "xero_tenant_id": "org-1",
        "access_token": "tok",
        "refresh_token": "rt",
        "expiry_date": "2099-01-01T00:00:00+00:00",
    }


def _http_resp(contacts: list) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"Contacts": contacts}
    return resp


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "contact_phones",
    [
        # exact E.164 in PhoneNumber
        [{"PhoneNumber": "+15551234567"}],
        # formatted string in PhoneNumber
        [{"PhoneNumber": "(555) 123-4567"}],
        [{"PhoneNumber": "555-123-4567"}],
        # split compound fields
        [{"PhoneCountryCode": "1", "PhoneAreaCode": "555", "PhoneNumber": "1234567"}],
        # area + number only
        [{"PhoneAreaCode": "555", "PhoneNumber": "123-4567"}],
        # match in 2nd phone, not 1st
        [{"PhoneNumber": "+19998887777"}, {"PhoneNumber": "+15551234567"}],
    ],
)
async def test_matches_various_xero_phone_shapes(contact_phones):
    contact = {"ContactID": "c1", "Name": "Test Caller", "Phones": contact_phones}
    client = MagicMock()
    client.get = AsyncMock(return_value=_http_resp([contact]))

    result = await xero_mod._get_contacts_by_phone(client, _cred(), "+15551234567")
    assert result is not None
    assert result["ContactID"] == "c1"


@pytest.mark.asyncio
async def test_does_not_match_different_number():
    contact = {
        "ContactID": "c1",
        "Phones": [{"PhoneNumber": "+19998887777"}],
    }
    client = MagicMock()
    client.get = AsyncMock(return_value=_http_resp([contact]))

    result = await xero_mod._get_contacts_by_phone(client, _cred(), "+15551234567")
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_no_phones():
    contact = {"ContactID": "c1", "Phones": []}
    client = MagicMock()
    client.get = AsyncMock(return_value=_http_resp([contact]))

    result = await xero_mod._get_contacts_by_phone(client, _cred(), "+15551234567")
    assert result is None
