"""Signature verification tests — Plan 39-06 fills these in."""
import pytest

pytestmark = pytest.mark.skipif(
    True,  # Flipped to False by Plan 39-06
    reason="security dependency not yet implemented (Plan 39-05, tested in 39-06)",
)


def test_valid_signature_returns_200(test_auth_token, monkeypatch):
    """Compute a valid X-Twilio-Signature, post to /twilio/incoming-call → 200."""
    pass


def test_invalid_signature_returns_403(monkeypatch):
    """Wrong signature → 403 with no ALLOW_UNSIGNED bypass."""
    pass


def test_missing_signature_header_returns_403(monkeypatch):
    """No X-Twilio-Signature header → 403."""
    pass


def test_allow_unsigned_env_var_bypasses_verification(monkeypatch):
    """ALLOW_UNSIGNED_WEBHOOKS=true → 200 with any/no signature."""
    pass
