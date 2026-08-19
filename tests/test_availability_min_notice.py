"""2026-08-19 — same-day minimum-notice floor in calc_slots_for_dates.

check_slot's too_soon branch enforces a 1-hour minimum notice on explicit
requests, but the slot calculator only drops slots that already STARTED — so
check_day / next_available_days (and check_slot's match/alternatives paths)
could offer, token-register, and ultimately book a slot starting minutes from
now. calc_slots_for_dates now applies MIN_NOTICE_TODAY_S to TODAY's slots
only; future days are untouched.

Property-based against the real clock (no frozen time): assertions hold at
any time of day, including the evening edge where today legitimately has no
remaining slots.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.tools._availability_lib import (
    MIN_NOTICE_TODAY_S,
    calc_slots_for_dates,
    tenant_today,
)

_TZ = "UTC"

_ALL_DAY_HOURS = {
    day: {"enabled": True, "open": "00:00", "close": "23:59"}
    for day in (
        "monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday",
    )
}

_TENANT = {
    "working_hours": _ALL_DAY_HOURS,
    "slot_duration_mins": 60,
    "travel_buffer_mins": 0,
}

_EMPTY_SCHED = {
    "appointments": [],
    "calendar_events": [],
    "calendar_blocks": [],
    "service_zones": [],
    "zone_travel_buffers": [],
}


def _start_dt(slot: dict) -> datetime:
    s = slot["start"]
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def test_min_notice_constant_is_at_least_one_hour():
    # check_slot's too_soon branch speaks "one hour minimum" — the shared
    # floor must never silently drop below it.
    assert MIN_NOTICE_TODAY_S >= 3600.0


def test_today_slots_respect_min_notice_floor():
    now_utc = datetime.now(timezone.utc)
    today = tenant_today(_TZ)
    slots = calc_slots_for_dates(_TENANT, [today], _EMPTY_SCHED, _TZ)
    # Small tolerance for the clock advancing between the floor computation
    # inside calc_slots_for_dates and `now_utc` above.
    floor = now_utc + timedelta(seconds=MIN_NOTICE_TODAY_S - 5)
    for slot in slots:
        assert _start_dt(slot) >= floor, (
            f"today-slot {slot['start']} starts inside the minimum-notice "
            f"window — check_day could offer a time book_appointment books "
            f"with under an hour's notice"
        )


def test_future_days_are_not_floored():
    now_utc = datetime.now(timezone.utc)
    tomorrow = (now_utc + timedelta(days=1)).strftime("%Y-%m-%d")
    slots = calc_slots_for_dates(_TENANT, [tomorrow], _EMPTY_SCHED, _TZ)
    assert slots, "tomorrow with all-day hours must have slots"
    # Tomorrow's first slot is midnight UTC — proves the floor is scoped to
    # today only and does not eat into future days.
    first = _start_dt(slots[0])
    assert (first.hour, first.minute) == (0, 0)
