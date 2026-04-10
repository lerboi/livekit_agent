"""Twilio signature verification tests — Plan 39-06.

Tests the verify_twilio_signature dependency via the real FastAPI app
(not a direct call). Uses twilio-python RequestValidator to compute valid
signatures and monkey-patches TWILIO_AUTH_TOKEN to a known value.

Test matrix:
  - Valid signature      -> 200
  - Invalid signature    -> 403
  - Missing header       -> 403
  - ALLOW_UNSIGNED=true  -> 200 with any/no signature
"""
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator


# ---------- Fixtures ----------


@pytest.fixture
def signed_client(monkeypatch, test_auth_token):
    """TestClient with a known auth token and signature verification ENABLED.

    ALLOW_UNSIGNED_WEBHOOKS is explicitly cleared so the dependency runs.
    """
    monkeypatch.delenv("ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", test_auth_token)
    # Import after env is set so module-level fetches (if any) see the token.
    from src.webhook.app import app
    # Clear any leftover dependency_overrides from other tests.
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _sign(auth_token: str, url: str, params: dict) -> str:
    """Compute an X-Twilio-Signature value for the given URL + POST params.

    Uses the canonical twilio-python RequestValidator.compute_signature,
    which is a public method in twilio>=9.0 (verified per RESEARCH.md §3).
    """
    validator = RequestValidator(auth_token)
    return validator.compute_signature(url, params)


# ---------- Tests ----------


def test_valid_signature_returns_200(signed_client, test_auth_token):
    """Compute a valid X-Twilio-Signature and post to /twilio/incoming-call -> 200."""
    params = {"To": "+15551234567", "From": "+15559876543"}
    # URL reconstruction in the dependency uses: f"{proto}://{host}{request.url.path}"
    # TestClient sends Host: "testserver" by default. We set x-forwarded-proto explicitly.
    host = "testserver"
    proto = "http"
    url = f"{proto}://{host}/twilio/incoming-call"
    signature = _sign(test_auth_token, url, params)

    resp = signed_client.post(
        "/twilio/incoming-call",
        data=params,
        headers={
            "X-Twilio-Signature": signature,
            "x-forwarded-proto": proto,
            "host": host,
        },
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    assert "<Dial>" in resp.text


def test_invalid_signature_returns_403(signed_client, test_auth_token):
    """Wrong signature -> 403 with no ALLOW_UNSIGNED bypass."""
    resp = signed_client.post(
        "/twilio/incoming-call",
        data={"To": "+15551234567", "From": "+15559876543"},
        headers={
            "X-Twilio-Signature": "definitely_not_a_valid_signature",
            "x-forwarded-proto": "http",
            "host": "testserver",
        },
    )
    assert resp.status_code == 403


def test_missing_signature_header_returns_403(signed_client):
    """No X-Twilio-Signature header -> 403."""
    resp = signed_client.post(
        "/twilio/incoming-call",
        data={"To": "+15551234567"},
        headers={
            "x-forwarded-proto": "http",
            "host": "testserver",
        },
    )
    assert resp.status_code == 403


def test_allow_unsigned_env_var_bypasses_verification(monkeypatch):
    """ALLOW_UNSIGNED_WEBHOOKS=true -> 200 with no signature at all."""
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "unused_in_bypass")
    from src.webhook.app import app
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        resp = client.post(
            "/twilio/incoming-call",
            data={"To": "+15551234567", "From": "+15559876543"},
            # No X-Twilio-Signature header at all
        )
    assert resp.status_code == 200
    assert "<Dial>" in resp.text
