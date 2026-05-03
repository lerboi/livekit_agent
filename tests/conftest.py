"""Shared pytest fixtures for livekit-agent unit tests.

Phase 60.2: adds mock_run_context and deps_factory so Fix H tests
can assert session.say() invocations without spinning up a real
LiveKit AgentSession.

Phase 60.3 Stream A: adds mock_diag_record, mock_agent_session, and
mock_deps_with_diag for the goodbye-race instrumentation tests
(tests/test_goodbye_diag.py).

Phase 61 Plan 02: adds gmaps_fixture loader for recorded Google Maps
Address Validation API response fixtures (tests/fixtures/gmaps_responses/).
"""
from __future__ import annotations

import json
from pathlib import Path
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


# ── Phase 61 Plan 02 fixtures ───────────────────────────────────────────────

_GMAPS_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "gmaps_responses"


@pytest.fixture
def gmaps_fixture():
    """Load a recorded Google Maps Address Validation API response by name.

    Usage:
        def test_something(gmaps_fixture):
            response = gmaps_fixture("us_confirmed")
            # response is the parsed JSON dict from
            # tests/fixtures/gmaps_responses/us_confirmed.json

    Available fixtures:
        - us_confirmed (verdict.possibleNextAction = ACCEPT)
        - us_confirm_with_corrections (CONFIRM with spellCorrected)
        - us_fix_required (FIX, addressComplete=false)
        - ca_confirmed (Canadian regionCode=CA)
        - sg_hdb_confirmed (SG with subpremise present)
        - sg_hdb_subpremise_missing (CONFIRM_ADD_SUBPREMISES)
        - unsupported_region_de (HTTP 400 INVALID_ARGUMENT body)
        - quota_exceeded_429 (HTTP 429 RESOURCE_EXHAUSTED body)
    """

    def _load(name: str) -> dict:
        path = _GMAPS_FIXTURE_DIR / f"{name}.json"
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    return _load
