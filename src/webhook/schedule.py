"""Pure-function schedule evaluator for call routing.

Called by the /twilio/incoming-call webhook handler (Phase 40 wires it into
the live TwiML branch; Phase 39 only defines the contract). No DB access,
no HTTP, no FastAPI imports — trivially unit-testable.

Locked decisions (see .planning/phases/39-call-routing-webhook-foundation/39-CONTEXT.md):
  D-05: schedule JSONB shape is {enabled:bool, days:{mon..sun:[{start,end}]}}
  D-06: in-range -> owner_pickup, outside all ranges -> ai, enabled:false -> always ai
  D-07: overnight ranges use end < start convention
  D-08: DST handled entirely by zoneinfo.astimezone()
  D-09: pure function, no DB access
  D-10: Phase 39 only emits 'ai' or 'owner_pickup'; 'fallback_to_ai' is Phase 40
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ScheduleDecision:
    mode: Literal["ai", "owner_pickup"]
    reason: Literal["schedule_disabled", "empty_schedule", "outside_window", "inside_window"]


_DAY_MAP = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}


def _in_range(local_hhmm: str, start: str, end: str) -> bool:
    """Check if local_hhmm (HH:MM) is within [start, end).

    Normal range (start <= end): inclusive start, exclusive end.
    Overnight range (end < start): matches times >= start OR < end (spans midnight).
    """
    if start <= end:
        return start <= local_hhmm < end
    # Overnight: start > end, e.g. "19:00"-"09:00"
    return local_hhmm >= start or local_hhmm < end


def evaluate_schedule(
    schedule: dict,
    tenant_timezone: str,
    now_utc: datetime,
) -> ScheduleDecision:
    """Pure function — no DB access. Called by /twilio/incoming-call handler.

    Args:
        schedule: call_forwarding_schedule JSONB value from tenants table.
                  Shape: {enabled: bool, days: {mon..sun: [{start, end}]}}
        tenant_timezone: IANA timezone string (e.g. "America/New_York")
        now_utc: UTC-aware datetime (datetime.now(tz=timezone.utc) at call time)

    Returns:
        ScheduleDecision with mode='ai' or 'owner_pickup' and a reason string.
    """
    if not schedule or not schedule.get("enabled", False):
        return ScheduleDecision(mode="ai", reason="schedule_disabled")

    days = schedule.get("days", {})
    if not days:
        return ScheduleDecision(mode="ai", reason="empty_schedule")

    # Convert UTC -> tenant local time. zoneinfo handles DST gaps/folds correctly.
    local_dt = now_utc.astimezone(ZoneInfo(tenant_timezone))
    day_key = _DAY_MAP[local_dt.weekday()]
    local_hhmm = local_dt.strftime("%H:%M")

    ranges = days.get(day_key, [])
    if not ranges:
        return ScheduleDecision(mode="ai", reason="outside_window")

    for r in ranges:
        start, end = r.get("start", ""), r.get("end", "")
        if not start or not end:
            continue
        if _in_range(local_hhmm, start, end):
            return ScheduleDecision(mode="owner_pickup", reason="inside_window")

    return ScheduleDecision(mode="ai", reason="outside_window")
