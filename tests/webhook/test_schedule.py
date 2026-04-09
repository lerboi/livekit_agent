"""Unit tests for evaluate_schedule() — Plan 39-03.

One test per row in RESEARCH.md §4 "Unit Test Fixture Table".
All tests are pure Python with datetime objects as input — no DB, no HTTP, no mocks.

DST reference timestamps (verified in RESEARCH.md §4 "Verified DST Timestamps"):
- UTC 2026-03-08 07:00 -> 03:00 EDT (New York spring-forward pivot)
- UTC 2026-11-01 06:30 -> 01:30 EST fold=1 (New York fall-back fold)
- Any UTC time -> +8 SGT (Singapore never observes DST)
"""
from datetime import datetime, timezone

import pytest

from src.webhook.schedule import ScheduleDecision, evaluate_schedule


# ---------- Disabled / empty cases ----------


def test_schedule_disabled_returns_ai():
    """enabled:false -> ai, reason=schedule_disabled."""
    result = evaluate_schedule(
        schedule={"enabled": False, "days": {"mon": [{"start": "09:00", "end": "17:00"}]}},
        tenant_timezone="America/New_York",
        now_utc=datetime(2026, 4, 6, 14, 0, tzinfo=timezone.utc),  # Mon 10:00 EDT
    )
    assert result == ScheduleDecision(mode="ai", reason="schedule_disabled")


def test_empty_schedule_dict_returns_ai():
    """{} -> ai, reason=schedule_disabled."""
    result = evaluate_schedule(
        schedule={},
        tenant_timezone="America/New_York",
        now_utc=datetime(2026, 4, 6, 14, 0, tzinfo=timezone.utc),
    )
    assert result == ScheduleDecision(mode="ai", reason="schedule_disabled")


def test_enabled_but_days_empty_returns_ai():
    """enabled:true, days:{} -> ai, reason=empty_schedule."""
    result = evaluate_schedule(
        schedule={"enabled": True, "days": {}},
        tenant_timezone="America/New_York",
        now_utc=datetime(2026, 4, 6, 14, 0, tzinfo=timezone.utc),
    )
    assert result == ScheduleDecision(mode="ai", reason="empty_schedule")


# ---------- Basic in/out of window ----------


def test_inside_window_returns_owner_pickup():
    """Mon 14:00 UTC = Mon 10:00 EDT, inside 09:00-17:00 window -> owner_pickup."""
    # 2026-04-06 is a Monday. Daylight saving is in effect -> EDT (UTC-4).
    result = evaluate_schedule(
        schedule={"enabled": True, "days": {"mon": [{"start": "09:00", "end": "17:00"}]}},
        tenant_timezone="America/New_York",
        now_utc=datetime(2026, 4, 6, 14, 0, tzinfo=timezone.utc),
    )
    assert result == ScheduleDecision(mode="owner_pickup", reason="inside_window")


def test_outside_window_returns_ai():
    """Mon 21:01 UTC = Mon 17:01 EDT, outside 09:00-17:00 window -> ai."""
    result = evaluate_schedule(
        schedule={"enabled": True, "days": {"mon": [{"start": "09:00", "end": "17:00"}]}},
        tenant_timezone="America/New_York",
        now_utc=datetime(2026, 4, 6, 21, 1, tzinfo=timezone.utc),
    )
    assert result == ScheduleDecision(mode="ai", reason="outside_window")


def test_day_with_no_ranges_returns_ai():
    """Mon call but only tue ranges defined -> ai, outside_window."""
    result = evaluate_schedule(
        schedule={"enabled": True, "days": {"tue": [{"start": "09:00", "end": "17:00"}]}},
        tenant_timezone="America/New_York",
        now_utc=datetime(2026, 4, 6, 14, 0, tzinfo=timezone.utc),  # Mon
    )
    assert result == ScheduleDecision(mode="ai", reason="outside_window")


# ---------- Boundary inclusivity ----------


def test_exact_start_boundary_inclusive():
    """Local 09:00 exactly with 09:00-17:00 range -> owner_pickup (start inclusive)."""
    # 2026-04-06 09:00 EDT = 13:00 UTC
    result = evaluate_schedule(
        schedule={"enabled": True, "days": {"mon": [{"start": "09:00", "end": "17:00"}]}},
        tenant_timezone="America/New_York",
        now_utc=datetime(2026, 4, 6, 13, 0, tzinfo=timezone.utc),
    )
    assert result == ScheduleDecision(mode="owner_pickup", reason="inside_window")


def test_exact_end_boundary_exclusive():
    """Local 17:00 exactly with 09:00-17:00 range -> ai (end exclusive)."""
    # 2026-04-06 17:00 EDT = 21:00 UTC
    result = evaluate_schedule(
        schedule={"enabled": True, "days": {"mon": [{"start": "09:00", "end": "17:00"}]}},
        tenant_timezone="America/New_York",
        now_utc=datetime(2026, 4, 6, 21, 0, tzinfo=timezone.utc),
    )
    assert result == ScheduleDecision(mode="ai", reason="outside_window")


def test_all_day_range():
    """00:00-23:59 matches any time -> owner_pickup."""
    result = evaluate_schedule(
        schedule={"enabled": True, "days": {"mon": [{"start": "00:00", "end": "23:59"}]}},
        tenant_timezone="America/New_York",
        now_utc=datetime(2026, 4, 6, 14, 0, tzinfo=timezone.utc),
    )
    assert result == ScheduleDecision(mode="owner_pickup", reason="inside_window")


# ---------- Overnight ranges ----------


def test_overnight_range_inside_evening():
    """Mon 20:00 EDT with 19:00-09:00 range -> owner_pickup (evening branch)."""
    # 2026-04-06 20:00 EDT = 2026-04-07 00:00 UTC
    result = evaluate_schedule(
        schedule={"enabled": True, "days": {"mon": [{"start": "19:00", "end": "09:00"}]}},
        tenant_timezone="America/New_York",
        now_utc=datetime(2026, 4, 7, 0, 0, tzinfo=timezone.utc),
    )
    assert result == ScheduleDecision(mode="owner_pickup", reason="inside_window")


def test_overnight_range_inside_morning():
    """Mon 08:00 EDT with mon 19:00-09:00 range -> owner_pickup (morning branch, same day key).

    Note: per RESEARCH.md §4 'Overnight range spanning-midnight note' (lines ~420-428),
    the simplest model looks up the CURRENT day's ranges. An 08:00 Monday local call
    with a mon 19:00-09:00 range returns owner_pickup because _in_range sees 08:00 < 09:00
    in the overnight branch. Phase 41's UI is expected to also write the same range under
    the following day if true cross-day matching is required — the evaluator itself does
    not synthesize cross-day lookups.
    """
    # 2026-04-06 08:00 EDT = 12:00 UTC
    result = evaluate_schedule(
        schedule={"enabled": True, "days": {"mon": [{"start": "19:00", "end": "09:00"}]}},
        tenant_timezone="America/New_York",
        now_utc=datetime(2026, 4, 6, 12, 0, tzinfo=timezone.utc),
    )
    assert result == ScheduleDecision(mode="owner_pickup", reason="inside_window")


def test_overnight_range_outside():
    """Mon 12:00 EDT with 19:00-09:00 range -> ai (midday outside overnight window)."""
    # 2026-04-06 12:00 EDT = 16:00 UTC
    result = evaluate_schedule(
        schedule={"enabled": True, "days": {"mon": [{"start": "19:00", "end": "09:00"}]}},
        tenant_timezone="America/New_York",
        now_utc=datetime(2026, 4, 6, 16, 0, tzinfo=timezone.utc),
    )
    assert result == ScheduleDecision(mode="ai", reason="outside_window")


# ---------- DST transitions ----------


def test_dst_spring_forward_new_york():
    """2026-03-08 07:00 UTC maps to 03:00 EDT (skips 02:xx). Sun 02:00-04:00 range includes 03:00 -> owner_pickup."""
    result = evaluate_schedule(
        schedule={"enabled": True, "days": {"sun": [{"start": "02:00", "end": "04:00"}]}},
        tenant_timezone="America/New_York",
        now_utc=datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc),
    )
    assert result == ScheduleDecision(mode="owner_pickup", reason="inside_window")


def test_dst_fall_back_new_york():
    """2026-11-01 06:30 UTC maps to 01:30 EST fold=1. Sun 01:00-02:00 range includes 01:30 -> owner_pickup."""
    result = evaluate_schedule(
        schedule={"enabled": True, "days": {"sun": [{"start": "01:00", "end": "02:00"}]}},
        tenant_timezone="America/New_York",
        now_utc=datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc),
    )
    assert result == ScheduleDecision(mode="owner_pickup", reason="inside_window")


def test_singapore_no_dst():
    """2026-03-08 04:00 UTC = 12:00 SGT. Sun 10:00-18:00 range includes 12:00 -> owner_pickup."""
    result = evaluate_schedule(
        schedule={"enabled": True, "days": {"sun": [{"start": "10:00", "end": "18:00"}]}},
        tenant_timezone="Asia/Singapore",
        now_utc=datetime(2026, 3, 8, 4, 0, tzinfo=timezone.utc),
    )
    assert result == ScheduleDecision(mode="owner_pickup", reason="inside_window")


# ---------- Multi-range days ----------


def test_multi_range_day_outside():
    """Mon 13:00 EDT with [08-12, 14-18] -> ai (between the two ranges)."""
    # 2026-04-06 13:00 EDT = 17:00 UTC
    result = evaluate_schedule(
        schedule={
            "enabled": True,
            "days": {"mon": [
                {"start": "08:00", "end": "12:00"},
                {"start": "14:00", "end": "18:00"},
            ]},
        },
        tenant_timezone="America/New_York",
        now_utc=datetime(2026, 4, 6, 17, 0, tzinfo=timezone.utc),
    )
    assert result == ScheduleDecision(mode="ai", reason="outside_window")


def test_multi_range_day_second_range():
    """Mon 15:00 EDT with [08-12, 14-18] -> owner_pickup (second range)."""
    # 2026-04-06 15:00 EDT = 19:00 UTC
    result = evaluate_schedule(
        schedule={
            "enabled": True,
            "days": {"mon": [
                {"start": "08:00", "end": "12:00"},
                {"start": "14:00", "end": "18:00"},
            ]},
        },
        tenant_timezone="America/New_York",
        now_utc=datetime(2026, 4, 6, 19, 0, tzinfo=timezone.utc),
    )
    assert result == ScheduleDecision(mode="owner_pickup", reason="inside_window")
