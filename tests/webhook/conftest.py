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
    """TestClient with signature dependency overridden to a form-reading no-op.

    Used for route tests that validate TwiML response bodies without
    exercising the signature layer. Plan 39-05 creates the app; until
    then the fixture skips gracefully.

    The override must still populate request.state.form_data because
    verify_twilio_signature is the single point in the request pipeline
    that parses the form body onto request.state (see src/webhook/security.py
    and src/webhook/twilio_routes.py which reads request.state.form_data).
    Returning a plain no-op would leave the attribute unset and crash the
    incoming-call handler — so the override replicates the form-stash step.
    """
    try:
        from fastapi import Request
        from fastapi.testclient import TestClient
        from src.webhook.app import app
        from src.webhook.security import verify_twilio_signature
    except ImportError:
        pytest.skip("webhook app not yet implemented (Plan 39-05)")

    async def _override(request: Request) -> None:
        # Mirror verify_twilio_signature's form-stash step without checking signatures.
        form_data = await request.form()
        request.state.form_data = dict(form_data)

    app.dependency_overrides[verify_twilio_signature] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def test_auth_token():
    """Fixed test auth token for signature-computation tests."""
    return "test_auth_token_12345"
