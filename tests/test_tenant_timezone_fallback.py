"""Phase 60.4 Stream A — tenant_timezone UTC-fallback + structured-WARN tests.

Three sites in production code fall back to a hard-coded "America/Chicago"
when tenants.tenant_timezone is NULL. That default silently miscalculates
SG bookings by 13 hours and is invisible in logs. Decision D-A-05 replaced
those fallbacks with "UTC" plus a structured WARN so operators can detect
unconfigured tenants during Phase 60.4 UAT.

Sites covered:
  - src/tools/book_appointment.py (assignment + WARN block)
  - src/tools/check_slot.py / check_day.py / next_available_days.py
    (inline `or "UTC"` — ported from the retired check_availability.py)
  - src/post_call.py (assignment + WARN block)
"""
from __future__ import annotations

import logging
import re


def test_book_appointment_null_tz_falls_back_to_UTC_with_warn(caplog):
    src = open(
        "C:/Users/leheh/.Projects/livekit-agent/src/tools/book_appointment.py",
        encoding="utf-8",
    ).read()
    # Structural assertion: the UTC-fallback block with a [tenant_config] WARN
    # must exist at this site (D-A-05).
    assert re.search(
        r"tenant_timezone\s*=\s*tenant\.get\(\"tenant_timezone\"\)\s+if\s+tenant\s+else\s+None",
        src,
    ), "book_appointment.py: null-aware tenant_timezone lookup missing"
    assert "[tenant_config] null tenant_timezone" in src, (
        "book_appointment.py: structured WARN substring missing"
    )
    assert 'tenant_timezone = "UTC"' in src, (
        "book_appointment.py: UTC fallback assignment missing"
    )
    # Guard: the old America/Chicago silent fallback must be gone at this site.
    assert 'or "America/Chicago"' not in src, (
        "book_appointment.py: legacy America/Chicago silent fallback still present"
    )


def test_availability_tools_null_tz_fall_back_to_UTC():
    """Ported 2026-06-10: check_availability.py was split into check_slot /
    check_day / next_available_days. The D-A-05 invariant they must keep is
    the UTC fallback (never the silent America/Chicago default that
    miscalculated SG bookings by 13 hours). The split tools use an inline
    `or "UTC"` fallback rather than the assignment+WARN block, so the WARN
    pin no longer applies at these sites (book_appointment/post_call keep
    theirs — see the sibling tests)."""
    for fname in (
        "src/tools/check_slot.py",
        "src/tools/check_day.py",
        "src/tools/next_available_days.py",
    ):
        src = open(
            f"C:/Users/leheh/.Projects/livekit-agent/{fname}",
            encoding="utf-8",
        ).read()
        assert 'or "UTC"' in src, (
            f"{fname}: UTC fallback for null tenant_timezone missing"
        )
        assert "America/Chicago" not in src, (
            f"{fname}: legacy America/Chicago silent fallback still present"
        )


def test_post_call_null_tz_falls_back_to_UTC_with_warn():
    src = open(
        "C:/Users/leheh/.Projects/livekit-agent/src/post_call.py",
        encoding="utf-8",
    ).read()
    assert "[tenant_config] null tenant_timezone" in src, (
        "post_call.py: structured WARN substring missing"
    )
    assert 'tenant_timezone = "UTC"' in src, (
        "post_call.py: UTC fallback assignment missing"
    )
    # post_call.py:612 previously had `tenant.get("tenant_timezone", "America/Chicago")`
    assert 'tenant.get("tenant_timezone", "America/Chicago")' not in src, (
        "post_call.py: legacy America/Chicago default-arg fallback still present"
    )
