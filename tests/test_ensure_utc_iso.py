"""Phase 60.4 Stream A — _ensure_utc_iso coerce-and-log tests (D-A-04).

Contract: _ensure_utc_iso receives ISO strings at the tool boundary. When
Gemini strips the UTC offset, the helper re-attaches "+00:00" and emits a
structured WARN (new in 60.4) so operators can see Gemini drift. The helper
MUST NEVER raise — otherwise a malformed ISO crashes the booking mid-call
(T-60.4-04).
"""
from __future__ import annotations

import logging

import pytest


def test_ensure_utc_iso_naive_warns_and_coerces(caplog):
    from src.tools.book_appointment import _ensure_utc_iso

    with caplog.at_level(logging.WARNING):
        out = _ensure_utc_iso("2026-04-30T07:00:00")

    assert out.endswith("+00:00"), f"coerced output should end with +00:00; got {out!r}"
    assert any(
        "[tz_coerce] naive ISO at boundary" in rec.getMessage()
        for rec in caplog.records
    ), (
        "expected a WARNING containing '[tz_coerce] naive ISO at boundary'; "
        f"got records: {[r.getMessage() for r in caplog.records]}"
    )


def test_ensure_utc_iso_offset_aware_passthrough(caplog):
    from src.tools.book_appointment import _ensure_utc_iso

    with caplog.at_level(logging.WARNING):
        out = _ensure_utc_iso("2026-04-30T07:00:00+00:00")

    # Offset-aware inputs must not emit the naive-boundary WARN.
    assert not any(
        "[tz_coerce] naive ISO at boundary" in rec.getMessage()
        for rec in caplog.records
    ), "offset-aware input should not emit the naive-boundary WARN"
    # Content-equivalent (tolerant of normalization to +00:00).
    assert out.endswith("+00:00")
    assert "2026-04-30T07:00:00" in out
