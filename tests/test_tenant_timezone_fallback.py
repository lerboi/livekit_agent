"""Phase 60.4 Stream A — tenant_timezone UTC-fallback + structured-WARN tests.

Three sites in production code fall back to a hard-coded "America/Chicago"
when tenants.tenant_timezone is NULL. That default silently miscalculates
SG bookings by 13 hours and is invisible in logs. Decision D-A-05 replaced
those fallbacks with "UTC" plus a structured WARN so operators can detect
unconfigured tenants during Phase 60.4 UAT.

Sites covered:
  - src/tools/book_appointment.py:266
  - src/tools/check_availability.py:142
  - src/post_call.py:612
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


def test_check_availability_null_tz_falls_back_to_UTC_with_warn():
    src = open(
        "C:/Users/leheh/.Projects/livekit-agent/src/tools/check_availability.py",
        encoding="utf-8",
    ).read()
    assert "[tenant_config] null tenant_timezone" in src, (
        "check_availability.py: structured WARN substring missing"
    )
    assert 'tenant_timezone = "UTC"' in src, (
        "check_availability.py: UTC fallback assignment missing"
    )
    assert 'or "America/Chicago"' not in src, (
        "check_availability.py: legacy America/Chicago silent fallback still present"
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
