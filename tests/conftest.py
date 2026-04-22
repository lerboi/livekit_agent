"""Shared pytest fixtures for livekit-agent unit tests.

Phase 60.2: adds mock_run_context and deps_factory so Fix H tests
can assert session.say() invocations without spinning up a real
LiveKit AgentSession.

Phase 60.3 Stream A: adds mock_diag_record, mock_agent_session, and
mock_deps_with_diag for the goodbye-race instrumentation tests
(tests/test_goodbye_diag.py).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_run_context():
    """Fake RunContext with an AsyncMock session.say. Default: awaits to None (success)."""
    session = SimpleNamespace()
    session.say = AsyncMock(return_value=None)
    # Leave room for later: other attrs can be set per-test via monkeypatch.
    ctx = SimpleNamespace()
    ctx.session = session
    return ctx


@pytest.fixture
def deps_factory():
    """Returns a factory producing fresh empty deps dicts (simulates separate sessions)."""
    def _make() -> dict:
        return {}
    return _make


# ── Phase 60.3 Stream A fixtures ────────────────────────────────────────────

@pytest.fixture
def mock_diag_record():
    """Fresh per-call diagnostic record — single-element list mirroring the
    agent.py runtime pattern at L189 (call_end_reason = ["caller_hangup"]).

    Returns a list with a single dict seeded with the keys the entrypoint
    would seed at call start, so tests can assert subsequent writes layer on
    top cleanly.
    """
    return [{
        "schema_version": 1,
        "call_id": "test-call-id",
        "tenant_id": "test-tenant-id",
        "caller_phone_sha256": "abcdef0123456789",
        "started_at_ms": 0,
    }]


@pytest.fixture
def mock_agent_session():
    """MagicMock AgentSession with the surfaces Stream A instrumentation touches.

    - .on("event", handler) → registered handlers collected on .on.call_args_list
    - .output.audio.capture_frame → AsyncMock so wrapper install tests can assert
      it was replaced and the replacement chains through on await.
    """
    session = MagicMock()
    session.on = MagicMock()
    session.output = MagicMock()
    session.output.audio = MagicMock()
    session.output.audio.capture_frame = AsyncMock(return_value=None)
    return session


@pytest.fixture
def mock_deps_with_diag(mock_diag_record):
    """deps dict pre-seeded with the three mutable-closure lists the
    entrypoint plumbs to every tool factory: call_end_reason, _tool_call_log,
    _diag_record. Matches the runtime shape at agent.py:192-221.
    """
    return {
        "call_id": "test-call-id",
        "tenant_id": "test-tenant-id",
        "from_number": "+6587528516",
        "call_end_reason": ["caller_hangup"],
        "_tool_call_log": [],
        "_diag_record": mock_diag_record,
        "room_name": "test-room",
        "sip_participant_identity": "sip_+6587528516",
    }
