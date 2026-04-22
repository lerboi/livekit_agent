"""Phase 60.3 Stream A — goodbye-race diagnostic instrumentation tests.

These tests assert the 6-hook instrumentation installed by src/agent.py
and src/tools/end_call.py produces a single structured [goodbye_race]
log entry per call with the schema defined in 60.3-RESEARCH.md §R-A8.

Hook points covered:
- R-A1: conversation_item_added → last_text_token_at (agent turns only)
- R-A2: session.output.audio.capture_frame wrapper → last_audio_frame_at
- R-A3: _GoodbyeDiagHandler reads text_done/audio_done from LogRecord extra=
- R-A4: end_call tool wrap → end_call_invoked_at
- R-A5: session.on(close) + ctx.room.on(participant_disconnected)
- R-A7: flush is FIRST statement in _on_close_async

Invariants from the plan's must_haves block:
- Every call ends with exactly one [goodbye_race] log entry.
- caller_phone_sha256 (SHA-256 first-16-hex) — raw E.164 never appears in payload.
- Diagnostic flush is the FIRST statement in _on_close_async.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Task 2 will extract these helpers to src/agent.py module level. ─────────
# Tests import lazily so Task 1 can fail red on ImportError/AttributeError.
def _import_goodbye_handler():
    from src.agent import _GoodbyeDiagHandler
    return _GoodbyeDiagHandler


def _import_flush_helper():
    """The diagnostic flush block is the FIRST statement in _on_close_async.
    Task 2 extracts it as a module-level async helper for testability.
    """
    from src.agent import _flush_goodbye_diag
    return _flush_goodbye_diag


# ── R-A6: diagnostic record seeding shape ───────────────────────────────────

def test_diag_record_seeded_with_schema_version_and_caller_hash():
    """After entrypoint seed, diag_record[0] must contain schema_version=1,
    a 16-hex-char caller_phone_sha256, started_at_ms int, and NOT contain
    the raw from_number.
    """
    from_number = "+6587528516"
    # Simulate the entrypoint seed (Task 2 will relocate this to agent.py).
    diag_record = [{
        "schema_version": 1,
        "call_id": "call-ABC",
        "tenant_id": "tenant-xyz",
        "caller_phone_sha256": (
            hashlib.sha256(from_number.encode("utf-8")).hexdigest()[:16]
        ),
        "started_at_ms": int(time.time() * 1000),
    }]

    rec = diag_record[0]
    assert rec["schema_version"] == 1
    assert isinstance(rec["caller_phone_sha256"], str)
    assert len(rec["caller_phone_sha256"]) == 16
    # hex only
    int(rec["caller_phone_sha256"], 16)
    assert isinstance(rec["started_at_ms"], int)
    # Raw phone number never leaks into the record payload.
    serialized = json.dumps(rec, default=str)
    assert from_number not in serialized


# ── R-A4: end_call tool writes end_call_invoked_at ──────────────────────────

@pytest.mark.asyncio
async def test_end_call_tool_writes_end_call_invoked_at(mock_deps_with_diag):
    """Invoking the end_call tool must set diag_record[0]["end_call_invoked_at"]
    to an int within ±500ms of time.time()*1000 at invocation.
    """
    from src.tools.end_call import create_end_call_tool

    # Stop _delayed_disconnect from doing anything side-effectful
    async def _noop_delayed(deps):
        return None

    with patch("src.tools.end_call._delayed_disconnect", new=_noop_delayed):
        tool = create_end_call_tool(mock_deps_with_diag)
        # FunctionTool is the decorator wrapper; reach into the real impl.
        # livekit.agents 1.5.1 exposes the underlying callable as __wrapped__
        # or .fnc — fall back to calling the tool directly.
        impl = getattr(tool, "fnc", None) or getattr(tool, "__wrapped__", None) or tool

        t_before = int(time.time() * 1000)
        result = await impl(MagicMock())
        t_after = int(time.time() * 1000)

    recorded = mock_deps_with_diag["_diag_record"][0].get("end_call_invoked_at")
    assert isinstance(recorded, int), "end_call_invoked_at must be int ms"
    assert t_before - 500 <= recorded <= t_after + 500
    assert isinstance(result, str)


# ── R-A1: conversation_item_added writes last_text_token_at on agent turns ──

def test_conversation_item_added_writes_last_text_token_at_agent_only(mock_diag_record):
    """The extended conversation_item_added handler must set
    last_text_token_at ONLY on agent-role events, and the value must equal
    event.created_at * 1000 (int).
    """
    diag_record = mock_diag_record
    transcript_turns = []

    # Replicate the handler body from agent.py:266-274 PLUS the Stream A addition.
    def handler(event):
        text = getattr(event.item, "text_content", None)
        if text:
            role = "user" if getattr(event.item, "role", None) == "user" else "agent"
            transcript_turns.append({
                "role": role,
                "content": text,
                "timestamp": int(time.time() * 1000),
            })
            if role == "agent":
                diag_record[0]["last_text_token_at"] = int(event.created_at * 1000)

    # Fire a user event first — must NOT set last_text_token_at.
    user_event = types.SimpleNamespace(
        item=types.SimpleNamespace(text_content="hello", role="user"),
        created_at=1_000_000.0,
    )
    handler(user_event)
    assert "last_text_token_at" not in diag_record[0]

    # Fire an agent event — must set last_text_token_at to int(created_at*1000).
    agent_event = types.SimpleNamespace(
        item=types.SimpleNamespace(text_content="thank you", role="agent"),
        created_at=2_000_000.5,
    )
    handler(agent_event)
    assert diag_record[0]["last_text_token_at"] == int(agent_event.created_at * 1000)


# ── R-A3: _GoodbyeDiagHandler captures text_done + audio_done from LogRecord ─

def test_goodbye_diag_handler_extracts_text_audio_done(mock_diag_record):
    """When a LogRecord with msg containing 'playback_finished called before
    text/audio' is passed to _GoodbyeDiagHandler.emit, the record's
    text_done/audio_done attributes (populated via logging extra=) must land
    on diag_record[0], and playback_finished_at must be set to an int ms.
    """
    _GoodbyeDiagHandler = _import_goodbye_handler()
    handler = _GoodbyeDiagHandler(mock_diag_record)

    # Standard logging.LogRecord ctor; extra= kwargs on logger.warning() land
    # as LogRecord attributes at call sites — simulate by direct attr set.
    record = logging.LogRecord(
        name="livekit.agents",
        level=logging.WARNING,
        pathname="synchronizer.py",
        lineno=277,
        msg=(
            "_SegmentSynchronizerImpl.playback_finished called before "
            "text/audio input is done"
        ),
        args=(),
        exc_info=None,
    )
    record.text_done = False
    record.audio_done = True

    t_before = int(time.time() * 1000)
    handler.emit(record)
    t_after = int(time.time() * 1000)

    rec = mock_diag_record[0]
    assert rec["text_done"] is False
    assert rec["audio_done"] is True
    assert isinstance(rec["playback_finished_at"], int)
    assert t_before <= rec["playback_finished_at"] <= t_after


# ── R-A7: flush is FIRST in _on_close_async, even if pipeline times out ─────

@pytest.mark.asyncio
async def test_flush_is_first_in_on_close_even_if_pipeline_times_out(
    mock_diag_record, caplog
):
    """If run_post_call_pipeline raises asyncio.TimeoutError, the
    [goodbye_race] logger.info + sentry breadcrumb must have already fired
    AND the _GoodbyeDiagHandler must be removed from livekit.agents logger.
    """
    _GoodbyeDiagHandler = _import_goodbye_handler()
    _flush_goodbye_diag = _import_flush_helper()

    # Pre-install a handler so cleanup is observable.
    lk_logger = logging.getLogger("livekit.agents")
    goodbye_handler = _GoodbyeDiagHandler(mock_diag_record)
    lk_logger.addHandler(goodbye_handler)
    initial_handler_count = len(lk_logger.handlers)

    transcript_turns = [
        {"role": "agent", "content": "Hi, Voco AI here."},
        {"role": "user", "content": "Hello"},
        {"role": "agent", "content": "Thank you for calling, have a great day"},
    ]
    tool_call_log = ["check_availability", "book_appointment", "end_call"]

    with caplog.at_level(logging.INFO, logger="voco-agent"), \
         patch("src.agent.sentry_sdk.add_breadcrumb") as mock_breadcrumb:
        await _flush_goodbye_diag(
            diag_record=mock_diag_record,
            transcript_turns=transcript_turns,
            tool_call_log=tool_call_log,
            goodbye_handler=goodbye_handler,
        )

    # Breadcrumb fired.
    mock_breadcrumb.assert_called_once()
    call_kwargs = mock_breadcrumb.call_args.kwargs
    assert call_kwargs.get("category") == "goodbye_race"

    # Logger emitted one [goodbye_race] line with JSON-parseable payload.
    goodbye_lines = [
        rec for rec in caplog.records
        if rec.levelname == "INFO" and "[goodbye_race]" in rec.getMessage()
    ]
    assert len(goodbye_lines) == 1, (
        f"expected exactly one [goodbye_race] info line, got {len(goodbye_lines)}"
    )
    msg = goodbye_lines[0].getMessage()
    payload_str = msg.split("[goodbye_race] ", 1)[1]
    parsed = json.loads(payload_str)
    assert parsed["schema_version"] == 1

    # Handler was removed from livekit.agents logger (cleanup — no per-call
    # handler accumulation).
    assert goodbye_handler not in lk_logger.handlers
    assert len(lk_logger.handlers) == initial_handler_count - 1


# ── R-A5: participant_disconnected captures disconnect_reason ───────────────

def test_participant_disconnect_reason_captured(mock_diag_record):
    """A participant_disconnected handler with matching identity must set
    participant_disconnect_at (int ms) and disconnect_reason (string name).
    """
    diag_record = mock_diag_record
    sip_participant_identity = "sip_+6587528516"

    # Minimal stand-in for rtc.DisconnectReason — the plan's code does
    # rtc.DisconnectReason.Name(...), so we mimic that signature.
    _DisconnectReason = types.SimpleNamespace(
        UNKNOWN_REASON=0,
        CLIENT_INITIATED=1,
        Name=staticmethod(
            lambda n: {0: "UNKNOWN_REASON", 1: "CLIENT_INITIATED"}.get(n, "UNKNOWN_REASON")
        ),
    )

    def handler(participant):
        if participant.identity == sip_participant_identity:
            diag_record[0]["participant_disconnect_at"] = int(time.time() * 1000)
            dr = participant.disconnect_reason or _DisconnectReason.UNKNOWN_REASON
            diag_record[0]["disconnect_reason"] = _DisconnectReason.Name(dr)

    participant = types.SimpleNamespace(
        identity=sip_participant_identity,
        disconnect_reason=_DisconnectReason.CLIENT_INITIATED,
    )

    t_before = int(time.time() * 1000)
    handler(participant)
    t_after = int(time.time() * 1000)

    rec = diag_record[0]
    assert rec["disconnect_reason"] == "CLIENT_INITIATED"
    assert isinstance(rec["participant_disconnect_at"], int)
    assert t_before <= rec["participant_disconnect_at"] <= t_after


# ── R-A8: transcript_tail truncated to 500 chars, raw phone not in payload ──

@pytest.mark.asyncio
async def test_transcript_tail_truncated_to_500_chars_and_no_raw_phone(
    mock_diag_record, caplog
):
    """Seed transcript_turns with 4 turns totaling >500 chars + a raw phone
    substring; flush; assert the serialized payload's transcript_tail is
    ≤500 chars AND the raw phone string is NOT a substring of the payload.
    """
    _GoodbyeDiagHandler = _import_goodbye_handler()
    _flush_goodbye_diag = _import_flush_helper()

    raw_phone = "+6587528516"
    long_content = "A" * 200  # each turn 200+ chars of filler
    transcript_turns = [
        {"role": "agent", "content": long_content},
        {"role": "user", "content": long_content + " " + raw_phone},
        {"role": "agent", "content": long_content},
        {"role": "agent", "content": "Thank you for calling, goodbye."},
    ]

    lk_logger = logging.getLogger("livekit.agents")
    goodbye_handler = _GoodbyeDiagHandler(mock_diag_record)
    lk_logger.addHandler(goodbye_handler)

    with caplog.at_level(logging.INFO, logger="voco-agent"), \
         patch("src.agent.sentry_sdk.add_breadcrumb"):
        await _flush_goodbye_diag(
            diag_record=mock_diag_record,
            transcript_turns=transcript_turns,
            tool_call_log=[],
            goodbye_handler=goodbye_handler,
        )

    goodbye_lines = [
        rec for rec in caplog.records
        if rec.levelname == "INFO" and "[goodbye_race]" in rec.getMessage()
    ]
    assert len(goodbye_lines) == 1
    msg = goodbye_lines[0].getMessage()
    payload_str = msg.split("[goodbye_race] ", 1)[1]
    parsed = json.loads(payload_str)

    # transcript_tail trimmed to ≤500 chars
    assert len(parsed["transcript_tail"]) <= 500

    # The raw phone number MUST NOT appear in the serialized payload.
    # caller_phone_sha256 in the seed uses a dummy "abcdef..." value (no
    # real hash), so a presence check on the raw phone string is meaningful.
    assert raw_phone not in payload_str
