"""Guided-choice availability returns (2026-06-11, findings.md P1).

The three availability tools used to return yes/no with no times by design
(a Gemini-era anti-fabrication guard). Production call 31559053 (2026-06-11)
showed the cost: "4 PM is too soon" → the agent could only ask the caller to
guess again → the caller hung up unbooked. The tools now pair every rejection
with the nearest tool-licensed alternative and let the agent OFFER times:

- check_day returns up to 3 representative windows (spread across the day),
  each with a registered slot_token.
- check_slot's too_soon branch returns the earliest viable time today (or the
  next opening within 2 days when today is done), with a token.
- check_slot's day_empty branch returns the next opening within 2 days.
- next_available_days returns the actual open-day labels.

The anti-hallucination invariant is unchanged: every speakable time comes
from a tool return; tokens resolve through the same deps["_slot_tokens"]
registry book_appointment already trusts.

All tests mock fetch_scheduling_data / calc_slots_for_dates — no DB.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tools._availability_lib import pick_spread


# ── Scaffolding ─────────────────────────────────────────────────────────────


def _avail_deps() -> dict:
    return {
        "supabase": MagicMock(),
        "tenant_id": "tenant-1",
        "call_id": "call-1",
        # Complete tenant dict so ensure_tenant() uses it without a DB hit.
        "tenant": {
            "tenant_timezone": "UTC",
            "working_hours": {},
            "slot_duration_mins": 60,
            "business_name": "Test Co",
        },
    }


def _slot(start_dt: datetime) -> dict:
    end_dt = start_dt + timedelta(hours=1)
    return {"start": start_dt.isoformat(), "end": end_dt.isoformat()}


def _slots_on(date_str: str, hhmm_list: list[str]) -> list[dict]:
    out = []
    for hhmm in hhmm_list:
        h, m = (int(x) for x in hhmm.split(":"))
        y, mo, d = (int(x) for x in date_str.split("-"))
        out.append(_slot(datetime(y, mo, d, h, m, tzinfo=timezone.utc)))
    return out


FUTURE_DATE = "2027-07-06"
FUTURE_NEXT = "2027-07-07"


# ── pick_spread ─────────────────────────────────────────────────────────────


def test_pick_spread_short_list_returns_all():
    slots = _slots_on(FUTURE_DATE, ["09:00", "14:00"])
    assert pick_spread(slots, 3) == sorted(slots, key=lambda s: s["start"])


def test_pick_spread_picks_first_middle_last():
    slots = _slots_on(FUTURE_DATE, ["09:00", "10:00", "11:00", "14:00", "16:00"])
    spread = pick_spread(slots, 3)
    assert [s["start"] for s in spread] == [
        slots[0]["start"], slots[2]["start"], slots[4]["start"],
    ]


def test_pick_spread_empty():
    assert pick_spread([], 3) == []


# ── check_day → OPTIONS with tokens ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_day_returns_spread_options_with_tokens():
    from src.tools.check_day import create_check_day_tool

    deps = _avail_deps()
    slots = _slots_on(FUTURE_DATE, ["09:00", "10:00", "11:00", "14:00", "16:00"])
    with patch(
        "src.tools.check_day.fetch_scheduling_data",
        new_callable=AsyncMock, return_value={},
    ), patch(
        "src.tools.check_day.calc_slots_for_dates", return_value=slots,
    ):
        tool = create_check_day_tool(deps)
        result = await tool.__wrapped__({"date": FUTURE_DATE}, MagicMock())

    assert result.startswith("STATE:day_has_slots")
    assert "count=5" in result
    assert "OPTIONS:" in result
    # Three spread options, each with its own registered token.
    assert result.count("token=slot_") == 3
    assert len(deps["_slot_tokens"]) == 3
    # Directive licenses offering, never reciting, and direct booking.
    assert "offer two or three" in result
    assert "books directly" in result


@pytest.mark.asyncio
async def test_check_day_empty_unchanged():
    from src.tools.check_day import create_check_day_tool

    deps = _avail_deps()
    with patch(
        "src.tools.check_day.fetch_scheduling_data",
        new_callable=AsyncMock, return_value={},
    ), patch(
        "src.tools.check_day.calc_slots_for_dates", return_value=[],
    ):
        tool = create_check_day_tool(deps)
        result = await tool.__wrapped__({"date": FUTURE_DATE}, MagicMock())

    assert result.startswith("STATE:day_empty")


# ── check_slot too_soon → earliest viable alternative ───────────────────────


@pytest.mark.asyncio
async def test_too_soon_offers_earliest_viable_today():
    from src.tools.check_slot import create_check_slot_tool
    from src.utils import to_local_date_string

    deps = _avail_deps()
    now = datetime.now(timezone.utc)
    today = to_local_date_string(now, "UTC")
    # Requested time ~5 min from now → violates the 1h minimum notice.
    requested = (now + timedelta(minutes=5)).strftime("%H:%M")
    # One slot inside the notice window (not viable) + one 3h out (viable).
    slots = [_slot(now + timedelta(minutes=30)), _slot(now + timedelta(hours=3))]

    with patch(
        "src.tools.check_slot.fetch_scheduling_data",
        new_callable=AsyncMock, return_value={},
    ), patch(
        "src.tools.check_slot.calc_slots_for_dates", return_value=slots,
    ):
        tool = create_check_slot_tool(deps)
        result = await tool.__wrapped__(
            {"date": today, "time": requested}, MagicMock()
        )

    assert result.startswith("STATE:too_soon")
    assert "earliest_today=" in result
    assert "token=slot_" in result
    # The offered alternative is bookable directly via _last_offered_token.
    assert deps["_last_offered_token"] in deps["_slot_tokens"]
    assert "same breath" in result


@pytest.mark.asyncio
async def test_too_soon_day_done_offers_next_opening():
    from src.tools.check_slot import create_check_slot_tool
    from src.utils import to_local_date_string

    deps = _avail_deps()
    now = datetime.now(timezone.utc)
    today = to_local_date_string(now, "UTC")
    requested = (now + timedelta(minutes=5)).strftime("%H:%M")
    tomorrow_slot = [_slot(now + timedelta(days=1))]

    def _calc(tenant, dates, sched, tz):
        # Nothing viable today; one opening tomorrow.
        return [] if dates == [today] else tomorrow_slot

    with patch(
        "src.tools.check_slot.fetch_scheduling_data",
        new_callable=AsyncMock, return_value={},
    ), patch(
        "src.tools.check_slot.calc_slots_for_dates", side_effect=_calc,
    ):
        tool = create_check_slot_tool(deps)
        result = await tool.__wrapped__(
            {"date": today, "time": requested}, MagicMock()
        )

    assert result.startswith("STATE:too_soon")
    assert "nothing_left_today=true" in result
    assert "next_open=" in result
    assert deps["_last_offered_token"] in deps["_slot_tokens"]


@pytest.mark.asyncio
async def test_too_soon_no_alternative_anywhere():
    from src.tools.check_slot import create_check_slot_tool
    from src.utils import to_local_date_string

    deps = _avail_deps()
    now = datetime.now(timezone.utc)
    today = to_local_date_string(now, "UTC")
    requested = (now + timedelta(minutes=5)).strftime("%H:%M")

    with patch(
        "src.tools.check_slot.fetch_scheduling_data",
        new_callable=AsyncMock, return_value={},
    ), patch(
        "src.tools.check_slot.calc_slots_for_dates", return_value=[],
    ):
        tool = create_check_slot_tool(deps)
        result = await tool.__wrapped__(
            {"date": today, "time": requested}, MagicMock()
        )

    assert result.startswith("STATE:too_soon")
    assert "nothing_left_today=true" in result
    assert "do not fabricate times" in result


# ── check_slot day_empty → next opening ─────────────────────────────────────


@pytest.mark.asyncio
async def test_day_empty_offers_next_opening():
    from src.tools.check_slot import create_check_slot_tool

    deps = _avail_deps()
    next_day_slots = _slots_on(FUTURE_NEXT, ["09:00"])

    def _calc(tenant, dates, sched, tz):
        return [] if dates == [FUTURE_DATE] else next_day_slots

    with patch(
        "src.tools.check_slot.fetch_scheduling_data",
        new_callable=AsyncMock, return_value={},
    ), patch(
        "src.tools.check_slot.calc_slots_for_dates", side_effect=_calc,
    ):
        tool = create_check_slot_tool(deps)
        result = await tool.__wrapped__(
            {"date": FUTURE_DATE, "time": "14:00"}, MagicMock()
        )

    assert result.startswith("STATE:day_empty")
    assert "next_open=" in result
    assert "token=slot_" in result
    assert deps["_last_offered_token"] in deps["_slot_tokens"]


# ── next_available_days → day labels ────────────────────────────────────────


@pytest.mark.asyncio
async def test_next_available_days_returns_open_day_labels():
    from src.tools.next_available_days import create_next_available_days_tool

    deps = _avail_deps()
    calls = {"n": 0}

    def _calc(tenant, dates, sched, tz):
        # Day 1: two slots; day 2: none; day 3: one slot.
        calls["n"] += 1
        if calls["n"] == 1:
            return _slots_on(FUTURE_DATE, ["09:00", "14:00"])
        if calls["n"] == 2:
            return []
        return _slots_on(FUTURE_NEXT, ["10:00"])

    with patch(
        "src.tools.next_available_days.fetch_scheduling_data",
        new_callable=AsyncMock, return_value={},
    ), patch(
        "src.tools.next_available_days.calc_slots_for_dates", side_effect=_calc,
    ):
        tool = create_next_available_days_tool(deps)
        result = await tool.__wrapped__({}, MagicMock())

    assert result.startswith("STATE:has_near_availability")
    assert "days=" in result
    # Two open days listed (day 2 had nothing).
    assert result.count("open)" ) == 2
    # Directive routes time-picking through check_day, never invention.
    assert "check_day" in result
    assert "do not invent times" in result


@pytest.mark.asyncio
async def test_next_available_days_empty_unchanged():
    from src.tools.next_available_days import create_next_available_days_tool

    deps = _avail_deps()
    with patch(
        "src.tools.next_available_days.fetch_scheduling_data",
        new_callable=AsyncMock, return_value={},
    ), patch(
        "src.tools.next_available_days.calc_slots_for_dates", return_value=[],
    ):
        tool = create_next_available_days_tool(deps)
        result = await tool.__wrapped__({}, MagicMock())

    assert result.startswith("STATE:no_near_availability")
