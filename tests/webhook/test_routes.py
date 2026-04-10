"""Integration tests for webhook routes — Plan 39-06.

Uses the client_no_auth fixture from conftest.py, which overrides the
signature verification dependency with a no-op. Signature behavior is
tested separately in test_security.py.

Tests run against the real FastAPI app via TestClient — no mocking of
FastAPI internals, only the /health/db route's Supabase call is allowed to
hit a (possibly missing) Supabase instance, in which case it returns 503.
"""
import json

import pytest


# ---------- Four Twilio endpoints ----------


def test_incoming_call_returns_ai_twiml(client_no_auth):
    """POST /twilio/incoming-call -> 200 with <Dial><Sip> TwiML.

    The handler performs a dead-weight tenant lookup (D-13) then returns the
    hardcoded AI TwiML branch. The test passes regardless of Supabase
    availability because the handler is fail-open.
    """
    resp = client_no_auth.post(
        "/twilio/incoming-call",
        data={"To": "+15551234567", "From": "+15559876543"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    body = resp.text
    assert "<Response>" in body
    assert "<Dial>" in body
    assert "<Sip>" in body


def test_dial_status_returns_empty_twiml(client_no_auth):
    """POST /twilio/dial-status -> 200 with empty <Response/> TwiML."""
    resp = client_no_auth.post(
        "/twilio/dial-status",
        data={"CallStatus": "completed", "DialCallDuration": "42"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    assert "<Response/>" in resp.text


def test_dial_fallback_returns_empty_twiml(client_no_auth):
    """POST /twilio/dial-fallback -> 200 with empty <Response/> TwiML."""
    resp = client_no_auth.post(
        "/twilio/dial-fallback",
        data={"ErrorCode": "11100"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    assert "<Response/>" in resp.text


def test_incoming_sms_returns_empty_twiml(client_no_auth):
    """POST /twilio/incoming-sms -> 200 with empty <Response/> TwiML."""
    resp = client_no_auth.post(
        "/twilio/incoming-sms",
        data={"From": "+15559876543", "To": "+15551234567", "Body": "Hello"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    assert "<Response/>" in resp.text


# ---------- Health endpoints ----------


def test_health_returns_ok(client_no_auth):
    """GET /health -> 200 with JSON {"status": "ok", "uptime": <int>, "version": <str>}."""
    resp = client_no_auth.get("/health")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body["status"] == "ok"
    assert "uptime" in body
    assert isinstance(body["uptime"], int)
    assert "version" in body
    assert isinstance(body["version"], str)


def test_health_db_returns_ok_or_503(client_no_auth):
    """GET /health/db -> 200 if DB reachable, 503 otherwise (both acceptable in tests).

    This test does not assume Supabase is reachable from the test environment.
    It only verifies that the route is mounted and returns one of the two
    valid response codes with a well-formed JSON body.
    """
    resp = client_no_auth.get("/health/db")
    assert resp.status_code in (200, 503)
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert "status" in body
    if resp.status_code == 200:
        assert body["status"] == "ok"
        assert body.get("db") == "connected"
    else:
        assert body["status"] == "error"
        assert "message" in body
