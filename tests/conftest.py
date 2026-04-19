"""Shared pytest fixtures for livekit-agent unit tests.

Phase 60.2: adds mock_run_context and deps_factory so Fix H tests
can assert session.say() invocations without spinning up a real
LiveKit AgentSession.
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
