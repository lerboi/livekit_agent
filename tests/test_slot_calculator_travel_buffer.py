"""M16 P2 — owner-adjustable travel buffer + forward/backward adjacency.

The slot calculator's default buffer is no longer a hardcoded 30: it reads the
tenant's `travel_buffer_mins` (threaded in as `travel_buffer_mins=`), and it is
now enforced on BOTH sides of every existing booking (forward + backward),
coordinate-free.

SHARED SCENARIO — kept identical to the JS twin
homeservice_agent/tests/scheduling/slot-calculator.test.js so both calculators
are proven to agree: tenant timezone UTC, working hours 09:00-17:00 (no lunch),
60-min slots, ONE existing booking 12:00-13:00 UTC. The only deliberate
difference from the JS fixture is the calendar date — JS pins a fake clock to a
Monday (2026-03-23); here every weekday is enabled and the date is computed in
the future so the working window has not elapsed against the real clock. Both
assert the SAME available start-hours (UTC) per buffer value:

    buffer 0  -> [9, 10, 11, 13, 14, 15, 16]   (buffering disabled)
    buffer 30 -> [9, 10, 14, 15, 16]            (default; both sides buffered)
    buffer 90 -> [9, 15, 16]                    (wide buffer pushes both sides out)
"""
from datetime import datetime, timedelta, timezone

from src.lib.slot_calculator import calculate_available_slots

UTC = "UTC"

WORKING_HOURS = {
    day: {"enabled": True, "open": "09:00", "close": "17:00"}
    for day in (
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    )
}


def _future_date(days_ahead: int = 30) -> str:
    d = datetime.now(timezone.utc) + timedelta(days=days_ahead)
    return f"{d.year}-{d.month:02d}-{d.day:02d}"


def _booking(target: str) -> list[dict]:
    return [{
        "start_time": f"{target}T12:00:00+00:00",
        "end_time": f"{target}T13:00:00+00:00",
    }]


def _start_hours(slots: list[dict]) -> list[int]:
    return [datetime.fromisoformat(s["start"]).astimezone(timezone.utc).hour for s in slots]


def _run(*, target: str, **overrides) -> list[dict]:
    kwargs = dict(
        working_hours=WORKING_HOURS,
        slot_duration_mins=60,
        existing_bookings=_booking(target),
        target_date=target,
        tenant_timezone=UTC,
        max_slots=20,
    )
    kwargs.update(overrides)
    return calculate_available_slots(**kwargs)


def test_buffer_zero_disables_buffering():
    target = _future_date()
    assert _start_hours(_run(target=target, travel_buffer_mins=0)) == [9, 10, 11, 13, 14, 15, 16]


def test_default_buffer_is_30_and_applies_both_sides():
    # travel_buffer_mins omitted -> defaults to 30 (proves the literal-30 value is preserved)
    target = _future_date()
    assert _start_hours(_run(target=target)) == [9, 10, 14, 15, 16]


def test_explicit_buffer_30_matches_default():
    target = _future_date()
    assert _start_hours(_run(target=target, travel_buffer_mins=30)) == [9, 10, 14, 15, 16]


def test_larger_buffer_pushes_slots_out_on_both_sides():
    target = _future_date()
    assert _start_hours(_run(target=target, travel_buffer_mins=90)) == [9, 15, 16]


def test_forward_case_withholds_slot_ending_at_booking_start():
    # 11:00-12:00 ends exactly when the 12:00 booking starts (gap 0 < 30) -> forward-buffered out;
    # 10:00-11:00 (gap 60 >= 30) survives. Backward-only logic would have offered 11:00.
    target = _future_date()
    hours = _start_hours(_run(target=target))
    assert 10 in hours
    assert 11 not in hours
