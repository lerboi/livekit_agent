"""Shared fixtures for webhook tests.

TestClient fixture bypasses signature verification via FastAPI's
dependency_overrides — the signature dependency is unit-tested separately
in test_security.py.
"""
import os
import pytest


@pytest.fixture
def unsigned_client(monkeypatch):
    """TestClient for tests that should bypass signature verification.

    Sets ALLOW_UNSIGNED_WEBHOOKS=true for the duration of the test.
    Plan 39-05 (Wave 2) creates src.webhook.app; until then, importing
    app will raise ImportError and tests will be collected but skipped.
    """
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test_token_unused_in_bypass")
    try:
        from fastapi.testclient import TestClient
        from src.webhook.app import app
    except ImportError:
        pytest.skip("webhook app not yet implemented (Plan 39-05)")
    return TestClient(app)


@pytest.fixture
def client_no_auth():
    """TestClient with signature dependency overridden to a no-op.

    Used for route tests that validate TwiML response bodies without
    exercising the signature layer. Plan 39-05 creates the app; until
    then the fixture skips gracefully.
    """
    try:
        from fastapi.testclient import TestClient
        from src.webhook.app import app
        from src.webhook.security import verify_twilio_signature
    except ImportError:
        pytest.skip("webhook app not yet implemented (Plan 39-05)")
    app.dependency_overrides[verify_twilio_signature] = lambda: None
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def test_auth_token():
    """Fixed test auth token for signature-computation tests."""
    return "test_auth_token_12345"
