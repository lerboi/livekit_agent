"""Phase 60.4 Stream A — Google Calendar event body TZ-correctness tests.

Pitfall 1 (from 60.4 RESEARCH): Google's `events.insert()` body must NOT mix
an offset-suffixed `dateTime` ("...+00:00") with a `timeZone` field — doing so
double-offsets the event. The fix is to pass a naive local ISO `dateTime`
alongside `timeZone`, letting Google interpret `dateTime` in the tenant TZ.

These tests pin the correct event-body shape so future edits cannot regress
to the Phase 60.3 behavior (caller asks 3 PM SG → calendar shows 11 PM SG).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _build_supabase_mock(tenant_timezone_in_db=None):
    """Build a chained MagicMock matching the supabase-py fluent API shape
    used inside google_calendar.push_booking_to_calendar.

    Order of table() calls inside the function:
      1. calendar_credentials   → creds row
      2. appointments           → appointment row
      3. tenants                → tenant row (business_name only)
      4. appointments (update external_event_id) — not asserted here
    """
    supabase = MagicMock()

    creds_exec = MagicMock()
    creds_exec.data = [{
        "access_token": "tok",
        "refresh_token": "rtok",
        "expiry_date": None,
        "calendar_id": "primary",
    }]

    appt_exec = MagicMock()
    appt_exec.data = [{
        "start_time": "2026-04-30T07:00:00+00:00",
        "end_time":   "2026-04-30T08:00:00+00:00",
        "service_address": "123 Orchard Rd",
        "caller_name": "Jia En",
        "urgency": "routine",
        "notes": None,
    }]

    tenant_exec = MagicMock()
    tenant_exec.data = [{"business_name": "ACME"}]

    update_exec = MagicMock()
    update_exec.data = []

    def table(name):
        tbl = MagicMock()
        if name == "calendar_credentials":
            tbl.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value = creds_exec
            tbl.update.return_value.eq.return_value.eq.return_value.execute.return_value = update_exec
        elif name == "appointments":
            tbl.select.return_value.eq.return_value.limit.return_value.execute.return_value = appt_exec
            tbl.update.return_value.eq.return_value.execute.return_value = update_exec
        elif name == "tenants":
            tbl.select.return_value.eq.return_value.limit.return_value.execute.return_value = tenant_exec
        return tbl

    supabase.table.side_effect = table
    return supabase


def _patch_google_service():
    """Return a (service_mock, insert_kwargs_capture) pair.
    insert_kwargs_capture is a list that will receive the `body=` kwargs
    passed to events().insert().
    """
    captured = {}
    service = MagicMock()

    insert_exec = MagicMock()
    insert_exec.execute.return_value = {"id": "evt_123"}

    def _insert(**kwargs):
        captured["body"] = kwargs.get("body")
        captured["calendarId"] = kwargs.get("calendarId")
        return insert_exec

    service.events.return_value.insert.side_effect = _insert
    return service, captured


def test_event_body_has_timeZone_field():
    """Pitfall 1 protection: both start and end carry timeZone = tenant_timezone."""
    from src.lib import google_calendar

    supabase = _build_supabase_mock()
    service, captured = _patch_google_service()

    with patch.object(google_calendar, "get_supabase_admin", return_value=supabase), \
         patch.object(google_calendar, "build", return_value=service), \
         patch.object(google_calendar, "Credentials"):
        google_calendar.push_booking_to_calendar(
            "tenant-1", "appt-1", "Asia/Singapore"
        )

    body = captured["body"]
    assert body is not None, "events.insert() not called"
    assert body["start"]["timeZone"] == "Asia/Singapore"
    assert body["end"]["timeZone"] == "Asia/Singapore"


def test_event_body_dateTime_is_naive_when_timezone_set():
    """Pitfall 1 inverted assertion: dateTime must NOT carry an offset suffix
    when timeZone is present — otherwise Google double-offsets the event.
    07:00 UTC in Asia/Singapore = 15:00 local (naive)."""
    from src.lib import google_calendar

    supabase = _build_supabase_mock()
    service, captured = _patch_google_service()

    with patch.object(google_calendar, "get_supabase_admin", return_value=supabase), \
         patch.object(google_calendar, "build", return_value=service), \
         patch.object(google_calendar, "Credentials"):
        google_calendar.push_booking_to_calendar(
            "tenant-1", "appt-1", "Asia/Singapore"
        )

    body = captured["body"]
    start_dt = body["start"]["dateTime"]
    assert "+" not in start_dt and not start_dt.endswith("Z"), (
        f"dateTime must be naive (no offset) when timeZone is set; got {start_dt!r}"
    )
    assert start_dt == "2026-04-30T15:00:00", (
        f"07:00 UTC → 15:00 Asia/Singapore (UTC+8); got {start_dt!r}"
    )
    end_dt = body["end"]["dateTime"]
    assert end_dt == "2026-04-30T16:00:00", (
        f"08:00 UTC → 16:00 Asia/Singapore; got {end_dt!r}"
    )


@pytest.mark.parametrize(
    "tz,expected_start",
    [
        ("Asia/Singapore", "2026-04-30T15:00:00"),  # UTC+8
        ("UTC", "2026-04-30T07:00:00"),             # UTC+0
        ("America/Chicago", "2026-04-30T02:00:00"), # CDT UTC-5 on 2026-04-30
    ],
)
def test_sg_tenant_utc_to_naive_local_correct(tz, expected_start):
    """_to_naive_local_iso produces correct naive local ISO per TZ."""
    from src.lib.google_calendar import _to_naive_local_iso

    assert _to_naive_local_iso("2026-04-30T07:00:00+00:00", tz) == expected_start
