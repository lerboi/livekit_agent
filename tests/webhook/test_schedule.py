"""Unit tests for evaluate_schedule() — Plan 39-03 fills these in."""
import pytest

pytestmark = pytest.mark.skipif(
    True,  # Flipped to False by Plan 39-03
    reason="evaluate_schedule not yet implemented (Plan 39-03)",
)


def test_schedule_disabled_returns_ai():
    """enabled:false → ai, reason=schedule_disabled."""
    pass


def test_empty_schedule_dict_returns_ai():
    """{} → ai, reason=schedule_disabled."""
    pass


def test_enabled_but_days_empty_returns_ai():
    """enabled:true, days:{} → ai, reason=empty_schedule."""
    pass


def test_inside_window_returns_owner_pickup():
    """Mon 14:00 UTC inside Mon 09:00-17:00 EST window → owner_pickup."""
    pass


def test_outside_window_returns_ai():
    """Mon 21:01 UTC = Mon 17:01 EDT, just outside Mon 09:00-17:00 window → ai."""
    pass


def test_day_with_no_ranges_returns_ai():
    """Mon call but only tue ranges defined → ai, outside_window."""
    pass


def test_exact_start_boundary_inclusive():
    """Local 09:00 exactly with 09:00-17:00 range → owner_pickup."""
    pass


def test_exact_end_boundary_exclusive():
    """Local 17:00 exactly with 09:00-17:00 range → ai (end exclusive)."""
    pass


def test_all_day_range():
    """00:00-23:59 range matches any time of day → owner_pickup."""
    pass


def test_overnight_range_inside_evening():
    """Mon 20:00 local with 19:00-09:00 range → owner_pickup."""
    pass


def test_overnight_range_inside_morning():
    """Mon 08:00 local with 19:00-09:00 range under 'mon' → owner_pickup."""
    pass


def test_overnight_range_outside():
    """Mon 12:00 local with 19:00-09:00 range → ai."""
    pass


def test_dst_spring_forward_new_york():
    """2026-03-08 07:00 UTC maps to 03:00 EDT (not the skipped 02:xx)."""
    pass


def test_dst_fall_back_new_york():
    """2026-11-01 06:30 UTC maps to 01:30 EST fold=1 → inside_window for 01:00-02:00."""
    pass


def test_singapore_no_dst():
    """2026-03-08 04:00 UTC → 12:00 SGT (no DST ever)."""
    pass


def test_multi_range_day_outside():
    """Mon 13:00 between 08-12 and 14-18 ranges → ai."""
    pass


def test_multi_range_day_second_range():
    """Mon 15:00 inside second 14-18 range → owner_pickup."""
    pass
