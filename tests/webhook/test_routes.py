"""Integration tests for webhook routes — Plan 39-06 fills these in."""
import pytest

pytestmark = pytest.mark.skipif(
    True,  # Flipped to False by Plan 39-06
    reason="webhook routes not yet implemented (Plan 39-05, tested in 39-06)",
)


def test_incoming_call_returns_ai_twiml(client_no_auth):
    """POST /twilio/incoming-call → 200, body contains <Response> and <Sip>."""
    pass


def test_dial_status_returns_empty_twiml(client_no_auth):
    """POST /twilio/dial-status → 200, body is <Response/>."""
    pass


def test_dial_fallback_returns_empty_twiml(client_no_auth):
    """POST /twilio/dial-fallback → 200, body is <Response/>."""
    pass


def test_incoming_sms_returns_empty_twiml(client_no_auth):
    """POST /twilio/incoming-sms → 200, body is <Response/>."""
    pass


def test_health_returns_ok(client_no_auth):
    """GET /health → 200, JSON {status: ok, ...}."""
    pass


def test_health_db_returns_ok_or_503(client_no_auth):
    """GET /health/db → 200 if DB reachable, 503 if not."""
    pass
