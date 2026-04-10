"""Integration tests for webhook routes — Plans 39-06 + 40-01.

Uses the client_no_auth fixture from conftest.py, which overrides the
signature verification dependency with a no-op. Signature behavior is
tested separately in test_security.py.

Tests run against the real FastAPI app via TestClient — no mocking of
FastAPI internals, only the /health/db route's Supabase call is allowed to
hit a (possibly missing) Supabase instance, in which case it returns 503.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.webhook.schedule import ScheduleDecision


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


def test_dial_fallback_returns_ai_twiml_basic(client_no_auth):
    """POST /twilio/dial-fallback -> 200 with AI SIP TwiML (Phase 40 live)."""
    resp = client_no_auth.post(
        "/twilio/dial-fallback",
        data={"ErrorCode": "11100"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    assert "<Sip>" in resp.text
    assert "<Dial>" in resp.text


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


# ---------- Phase 40 — incoming-call routing tests ----------


def _make_tenant_mock(
    tenant_data: dict | None,
    *,
    subscriptions: list[dict] | None = None,
):
    """Build a MagicMock supabase chain returning a single tenant row.

    The mock mimics:
      supabase.table("tenants").select(...).eq(...).limit(1).execute()
    """
    row = None
    if tenant_data is not None:
        row = {**tenant_data}
        if subscriptions is not None:
            row["subscriptions"] = subscriptions
        elif "subscriptions" not in row:
            row["subscriptions"] = [{"status": "active"}]

    response = MagicMock()
    response.data = [row] if row else []

    chain = MagicMock()
    chain.execute.return_value = response
    chain.limit.return_value = chain
    chain.eq.return_value = chain
    chain.select.return_value = chain

    supabase = MagicMock()
    supabase.table.return_value = chain
    return supabase


def _patch_routing(monkeypatch, *, tenant_data=None, subscriptions=None,
                   schedule_decision=None, cap_ok=True):
    """Apply all monkeypatches for the incoming-call routing tests."""
    mock_sb = _make_tenant_mock(tenant_data, subscriptions=subscriptions)
    monkeypatch.setattr(
        "src.supabase_client.get_supabase_admin",
        lambda: mock_sb,
    )
    if schedule_decision is not None:
        monkeypatch.setattr(
            "src.webhook.twilio_routes.evaluate_schedule",
            lambda sched, tz, now: schedule_decision,
        )
    monkeypatch.setattr(
        "src.webhook.twilio_routes.check_outbound_cap",
        AsyncMock(return_value=cap_ok),
    )
    monkeypatch.setenv("RAILWAY_WEBHOOK_URL", "https://test.example.com")
    monkeypatch.setenv("LIVEKIT_SIP_URI", "sip:test@sip.livekit.cloud")
    return mock_sb


def test_incoming_call_ai_mode(client_no_auth, monkeypatch):
    """POST /twilio/incoming-call with schedule enabled=false -> AI TwiML."""
    tenant = {
        "id": "t-ai",
        "call_forwarding_schedule": {"enabled": False, "days": {}},
        "tenant_timezone": "UTC",
        "country": "US",
        "pickup_numbers": [],
        "dial_timeout_seconds": 15,
    }
    _patch_routing(
        monkeypatch,
        tenant_data=tenant,
        schedule_decision=ScheduleDecision(mode="ai", reason="schedule_disabled"),
    )
    resp = client_no_auth.post(
        "/twilio/incoming-call",
        data={"To": "+15551234567", "From": "+15559876543", "CallSid": "CA0001"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "<Sip>" in body
    assert "sip:test@sip.livekit.cloud" in body


def test_incoming_call_owner_pickup(client_no_auth, monkeypatch):
    """POST /twilio/incoming-call with owner_pickup schedule, under cap, 2 numbers -> Dial TwiML."""
    tenant = {
        "id": "t-pickup",
        "call_forwarding_schedule": {"enabled": True, "days": {"mon": [{"start": "08:00", "end": "17:00"}]}},
        "tenant_timezone": "America/New_York",
        "country": "US",
        "pickup_numbers": [{"number": "+1111"}, {"number": "+2222"}],
        "dial_timeout_seconds": 15,
    }
    _patch_routing(
        monkeypatch,
        tenant_data=tenant,
        schedule_decision=ScheduleDecision(mode="owner_pickup", reason="inside_window"),
        cap_ok=True,
    )
    resp = client_no_auth.post(
        "/twilio/incoming-call",
        data={"To": "+15551234567", "From": "+15559876543", "CallSid": "CA0002"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "<Dial" in body
    assert 'timeout="15"' in body
    assert "<Number>+1111</Number>" in body
    assert "<Number>+2222</Number>" in body
    assert "action=" in body
    assert "/twilio/dial-status" in body


def test_incoming_call_unknown_tenant(client_no_auth, monkeypatch):
    """POST /twilio/incoming-call with unknown To number -> AI TwiML (fail-open)."""
    _patch_routing(
        monkeypatch,
        tenant_data=None,  # no tenant found
    )
    resp = client_no_auth.post(
        "/twilio/incoming-call",
        data={"To": "+10000000000", "From": "+15559876543", "CallSid": "CA0003"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "<Sip>" in body


def test_incoming_call_blocked_tenant(client_no_auth, monkeypatch):
    """POST /twilio/incoming-call with canceled subscription -> AI TwiML (fail-open per D-01)."""
    tenant = {
        "id": "t-blocked",
        "call_forwarding_schedule": {"enabled": True, "days": {"mon": [{"start": "08:00", "end": "17:00"}]}},
        "tenant_timezone": "UTC",
        "country": "US",
        "pickup_numbers": [{"number": "+1111"}],
        "dial_timeout_seconds": 15,
    }
    _patch_routing(
        monkeypatch,
        tenant_data=tenant,
        subscriptions=[{"status": "canceled"}],
        schedule_decision=ScheduleDecision(mode="owner_pickup", reason="inside_window"),
    )
    resp = client_no_auth.post(
        "/twilio/incoming-call",
        data={"To": "+15551234567", "From": "+15559876543", "CallSid": "CA0004"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "<Sip>" in body


def test_incoming_call_cap_breach(client_no_auth, monkeypatch):
    """POST /twilio/incoming-call with owner_pickup but cap breached -> AI TwiML."""
    tenant = {
        "id": "t-cap",
        "call_forwarding_schedule": {"enabled": True, "days": {"mon": [{"start": "08:00", "end": "17:00"}]}},
        "tenant_timezone": "UTC",
        "country": "US",
        "pickup_numbers": [{"number": "+1111"}],
        "dial_timeout_seconds": 15,
    }
    _patch_routing(
        monkeypatch,
        tenant_data=tenant,
        schedule_decision=ScheduleDecision(mode="owner_pickup", reason="inside_window"),
        cap_ok=False,
    )
    resp = client_no_auth.post(
        "/twilio/incoming-call",
        data={"To": "+15551234567", "From": "+15559876543", "CallSid": "CA0005"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "<Sip>" in body


def test_incoming_call_no_pickup_numbers(client_no_auth, monkeypatch):
    """POST /twilio/incoming-call with owner_pickup but empty pickup_numbers -> AI TwiML."""
    tenant = {
        "id": "t-nopickup",
        "call_forwarding_schedule": {"enabled": True, "days": {"mon": [{"start": "08:00", "end": "17:00"}]}},
        "tenant_timezone": "UTC",
        "country": "US",
        "pickup_numbers": [],
        "dial_timeout_seconds": 15,
    }
    _patch_routing(
        monkeypatch,
        tenant_data=tenant,
        schedule_decision=ScheduleDecision(mode="owner_pickup", reason="inside_window"),
        cap_ok=True,
    )
    resp = client_no_auth.post(
        "/twilio/incoming-call",
        data={"To": "+15551234567", "From": "+15559876543", "CallSid": "CA0006"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "<Sip>" in body


def test_owner_pickup_twiml_structure(client_no_auth, monkeypatch):
    """Verify _owner_pickup_twiml builds correct XML structure."""
    from src.webhook.twilio_routes import _owner_pickup_twiml
    monkeypatch.setenv("RAILWAY_WEBHOOK_URL", "https://test.example.com")

    result = _owner_pickup_twiml("+15559876543", ["+1111", "+2222", "+3333"], 20)
    assert '<?xml version="1.0"' in result
    assert "<Response>" in result
    assert '<Dial timeout="20"' in result
    assert 'callerId="+15559876543"' in result
    assert "<Number>+1111</Number>" in result
    assert "<Number>+2222</Number>" in result
    assert "<Number>+3333</Number>" in result
    assert 'action="https://test.example.com/twilio/dial-status"' in result

    # Max 5 numbers
    result5 = _owner_pickup_twiml("+1", [f"+{i}" for i in range(10)], 15)
    assert result5.count("<Number>") == 5


def test_owner_pickup_inserts_calls_row(client_no_auth, monkeypatch):
    """Verify owner_pickup routing inserts a calls row with call_sid and routing_mode."""
    insert_mock = MagicMock()
    insert_mock.execute.return_value = MagicMock(data=[{"id": "call-1"}])

    tenant = {
        "id": "t-insert",
        "call_forwarding_schedule": {"enabled": True, "days": {"mon": [{"start": "08:00", "end": "17:00"}]}},
        "tenant_timezone": "UTC",
        "country": "US",
        "pickup_numbers": [{"number": "+1111"}],
        "dial_timeout_seconds": 15,
    }
    mock_sb = _patch_routing(
        monkeypatch,
        tenant_data=tenant,
        schedule_decision=ScheduleDecision(mode="owner_pickup", reason="inside_window"),
        cap_ok=True,
    )
    # Wire the insert mock
    mock_sb.table.return_value.insert.return_value = insert_mock

    resp = client_no_auth.post(
        "/twilio/incoming-call",
        data={"To": "+15551234567", "From": "+15559876543", "CallSid": "CA0007"},
    )
    assert resp.status_code == 200
    assert "<Dial" in resp.text

    # Verify insert was called with correct data
    insert_call = mock_sb.table.return_value.insert
    assert insert_call.called
    insert_args = insert_call.call_args[0][0]
    assert insert_args["tenant_id"] == "t-insert"
    assert insert_args["call_sid"] == "CA0007"
    assert insert_args["routing_mode"] == "owner_pickup"


# ---------- Phase 40-02 — dial-status and dial-fallback tests ----------


def _make_update_mock():
    """Build a MagicMock supabase chain for update().eq().execute()."""
    response = MagicMock()
    response.data = [{"id": "call-1"}]

    chain = MagicMock()
    chain.execute.return_value = response
    chain.eq.return_value = chain
    chain.update.return_value = chain

    supabase = MagicMock()
    supabase.table.return_value = chain
    return supabase


def test_dial_status_updates_calls_row(client_no_auth, monkeypatch):
    """POST /twilio/dial-status with completed call -> updates calls row with
    routing_mode=owner_pickup and outbound_dial_duration_sec=45."""
    mock_sb = _make_update_mock()
    monkeypatch.setattr(
        "src.supabase_client.get_supabase_admin",
        lambda: mock_sb,
    )
    resp = client_no_auth.post(
        "/twilio/dial-status",
        data={
            "CallSid": "CS123",
            "DialCallStatus": "completed",
            "DialCallDuration": "45",
        },
    )
    assert resp.status_code == 200
    assert "<Response/>" in resp.text

    # Verify update was called with correct data
    update_call = mock_sb.table.return_value.update
    assert update_call.called
    update_data = update_call.call_args[0][0]
    assert update_data["routing_mode"] == "owner_pickup"
    assert update_data["outbound_dial_duration_sec"] == 45

    # Verify eq filter on call_sid
    eq_call = mock_sb.table.return_value.update.return_value.eq
    assert eq_call.called
    eq_args = eq_call.call_args[0]
    assert eq_args == ("call_sid", "CS123")


def test_dial_status_no_answer(client_no_auth, monkeypatch):
    """POST /twilio/dial-status with no-answer -> routing_mode=fallback_to_ai, no duration."""
    mock_sb = _make_update_mock()
    monkeypatch.setattr(
        "src.supabase_client.get_supabase_admin",
        lambda: mock_sb,
    )
    resp = client_no_auth.post(
        "/twilio/dial-status",
        data={
            "CallSid": "CS456",
            "DialCallStatus": "no-answer",
        },
    )
    assert resp.status_code == 200
    assert "<Response/>" in resp.text

    update_data = mock_sb.table.return_value.update.call_args[0][0]
    assert update_data["routing_mode"] == "fallback_to_ai"
    assert "outbound_dial_duration_sec" not in update_data


def test_dial_status_busy(client_no_auth, monkeypatch):
    """POST /twilio/dial-status with busy -> routing_mode=fallback_to_ai."""
    mock_sb = _make_update_mock()
    monkeypatch.setattr(
        "src.supabase_client.get_supabase_admin",
        lambda: mock_sb,
    )
    resp = client_no_auth.post(
        "/twilio/dial-status",
        data={
            "CallSid": "CS789",
            "DialCallStatus": "busy",
        },
    )
    assert resp.status_code == 200

    update_data = mock_sb.table.return_value.update.call_args[0][0]
    assert update_data["routing_mode"] == "fallback_to_ai"


def test_dial_status_db_failure(client_no_auth, monkeypatch):
    """POST /twilio/dial-status when DB raises Exception -> still returns empty TwiML (fail-safe)."""
    def _raise():
        raise RuntimeError("DB connection lost")

    mock_sb = MagicMock()
    mock_sb.table.return_value.update.side_effect = RuntimeError("DB connection lost")
    monkeypatch.setattr(
        "src.supabase_client.get_supabase_admin",
        lambda: mock_sb,
    )
    resp = client_no_auth.post(
        "/twilio/dial-status",
        data={
            "CallSid": "CS999",
            "DialCallStatus": "completed",
            "DialCallDuration": "10",
        },
    )
    assert resp.status_code == 200
    assert "<Response/>" in resp.text


def test_dial_fallback_returns_ai_twiml(client_no_auth, monkeypatch):
    """POST /twilio/dial-fallback -> response contains <Sip> (AI TwiML), not empty TwiML."""
    monkeypatch.setenv("LIVEKIT_SIP_URI", "sip:test@sip.livekit.cloud")
    resp = client_no_auth.post(
        "/twilio/dial-fallback",
        data={"ErrorCode": "11100"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "<Sip>" in body
    assert "<Dial>" in body
    assert "sip:test@sip.livekit.cloud" in body
    assert "<Response/>" not in body
