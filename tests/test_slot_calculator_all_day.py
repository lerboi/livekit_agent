"""All-day external blocks must block tenant-LOCAL day bounds, not the literal
UTC-midnight timestamps the mirror rows store (which blocked 08:00→08:00-next-day
for Asia/Singapore tenants)."""
from datetime import datetime, timedelta, timezone

from src.lib.slot_calculator import _all_day_busy_bounds, calculate_available_slots

SG = "Asia/Singapore"

WORKING_HOURS = {
    day: {"enabled": True, "open": "09:00", "close": "17:00"}
    for day in (
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    )
}


def _future_date(days_ahead: int = 30) -> str:
    d = datetime.now(timezone.utc) + timedelta(days=days_ahead)
    return f"{d.year}-{d.month:02d}-{d.day:02d}"


def test_all_day_bounds_google_style_exclusive_end():
    """Google one-day all-day event: start=00:00Z of day D, end=00:00Z of D+1.
    Must expand to exactly [00:00 local D, 00:00 local D+1) — not two days."""
    start = datetime(2026, 6, 10, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 11, 0, 0, tzinfo=timezone.utc)
    busy_start, busy_end = _all_day_busy_bounds(start, end, SG)
    # 00:00 SG = previous day 16:00 UTC
    assert busy_start == datetime(2026, 6, 9, 16, 0, tzinfo=timezone.utc)
    assert busy_end == datetime(2026, 6, 10, 16, 0, tzinfo=timezone.utc)


def test_all_day_bounds_same_date_dashboard_block():
    """Dashboard all-day calendar_block: 07:00→20:00 on the same date — must
    cover that one full local day."""
    start = datetime(2026, 6, 10, 7, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 10, 20, 0, tzinfo=timezone.utc)
    busy_start, busy_end = _all_day_busy_bounds(start, end, SG)
    assert busy_start == datetime(2026, 6, 9, 16, 0, tzinfo=timezone.utc)
    assert busy_end == datetime(2026, 6, 10, 16, 0, tzinfo=timezone.utc)


def test_all_day_event_blocks_entire_local_day():
    target = _future_date()
    year, month, day = (int(x) for x in target.split("-"))
    next_day = datetime(year, month, day, tzinfo=timezone.utc) + timedelta(days=1)

    blocks = [{
        "start_time": f"{target}T00:00:00+00:00",
        "end_time": next_day.strftime("%Y-%m-%dT00:00:00+00:00"),
        "is_all_day": True,
    }]
    slots = calculate_available_slots(
        working_hours=WORKING_HOURS,
        slot_duration_mins=60,
        external_blocks=blocks,
        target_date=target,
        tenant_timezone=SG,
    )
    assert slots == []


def test_timed_event_unaffected_by_all_day_expansion():
    """A non-all-day mirror row keeps literal timestamp comparison."""
    target = _future_date(31)
    blocks = [{
        # 01:00-02:00 UTC = 09:00-10:00 SG — should block only the first slot
        "start_time": f"{target}T01:00:00+00:00",
        "end_time": f"{target}T02:00:00+00:00",
        "is_all_day": False,
    }]
    slots = calculate_available_slots(
        working_hours=WORKING_HOURS,
        slot_duration_mins=60,
        external_blocks=blocks,
        target_date=target,
        tenant_timezone=SG,
    )
    assert len(slots) == 7  # 09:00-17:00 minus the one blocked hour
