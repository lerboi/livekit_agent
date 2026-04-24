"""
Shared helpers for the three availability tools: check_slot, check_day,
next_available_days. Extracted from the former monolithic check_availability.py
so each narrow tool imports only what it needs and payload/description sizes
stay small for Gemini 3.1 Flash Live (see
.planning/research/check-availability-split-plan.md).

Nothing in this module is a tool — tools live in their own files and call
into here. No behavior change from the pre-split implementation.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..lib.slot_calculator import calculate_available_slots
from ..utils import to_local_date_string, format_zone_pair_buffers

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Slot-cache + slot-token constants (unchanged from pre-split).
# deps["_slot_cache"] TTL narrows the check_slot / check_day live-fetch window
# so Gemini Live's server-side VAD has less time to cancel the tool call.
# deps["_slot_tokens"] maps an opaque "slot_xxxxxxxx" to UTC (start, end) so
# book_appointment can resolve without trusting any Gemini-reconstructed ISO.
# ─────────────────────────────────────────────────────────────────────────────
SLOT_CACHE_TTL_S = 30.0
SLOT_TOKEN_TTL_S = 600.0


# ─────────────────────────────────────────────────────────────────────────────
# Slot-token registry
# ─────────────────────────────────────────────────────────────────────────────

def register_slot_token(deps: dict, slot_start: str, slot_end: str) -> str:
    """Mint an opaque token bound to a (slot_start_utc, slot_end_utc) pair.
    8 hex chars = 32 bits, collision-proof for the ~10 tokens a call produces."""
    token = "slot_" + secrets.token_hex(4)
    tokens = deps.setdefault("_slot_tokens", {})
    tokens[token] = {
        "slot_start_utc": slot_start,
        "slot_end_utc": slot_end,
        "created_at": _time.time(),
    }
    return token


# ─────────────────────────────────────────────────────────────────────────────
# Date / time formatting
# ─────────────────────────────────────────────────────────────────────────────

def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def format_date_label(date_str: str, tenant_timezone: str) -> str:
    """'2026-04-28' + 'America/Chicago' -> 'Tuesday, April 28th'."""
    year, month, day = (int(x) for x in date_str.split("-"))
    dt = datetime(year, month, day, 12, 0, 0, tzinfo=ZoneInfo(tenant_timezone))
    return f"{dt.strftime('%A')}, {dt.strftime('%B')} {_ordinal(dt.day)}"


def parse_hhmm_to_utc(time_str: str, date_str: str, tenant_timezone: str) -> datetime | None:
    """Parse 'HH:MM' (24-hour) for the given YYYY-MM-DD date in the tenant's
    timezone; return the instant as UTC. Returns None on malformed input.

    Schema-enforced pattern (check_slot raw_schema) means this only needs to
    handle the HH:MM shape, but we retain belt-and-braces 12-hour parsing in
    case Gemini bypasses the schema — rare, but the audit noted Phase 63.1-08/
    09/10 all fought schema-vs-prose drift.
    """
    s = time_str.strip().lower()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
    else:
        m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
        if not m:
            return None
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        ampm = m.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    year, month, day = (int(x) for x in date_str.split("-"))
    local_dt = datetime(year, month, day, hour, minute, 0, tzinfo=ZoneInfo(tenant_timezone))
    return local_dt.astimezone(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Tenant + scheduling data fetch
# ─────────────────────────────────────────────────────────────────────────────

_NEEDED_TENANT_FIELDS = (
    "tenant_timezone",
    "working_hours",
    "slot_duration_mins",
    "business_name",
)


async def ensure_tenant(deps: dict) -> dict | None:
    """Return the tenant dict. Prefers the session-init fetch already on deps;
    re-queries only when the cached value is missing a required field."""
    tenant = deps.get("tenant")
    if tenant and all(k in tenant for k in _NEEDED_TENANT_FIELDS):
        return tenant

    tenant_id = deps.get("tenant_id")
    supabase = deps["supabase"]
    if not tenant_id:
        return None

    try:
        result = await asyncio.to_thread(
            lambda: supabase.table("tenants")
            .select("tenant_timezone, working_hours, slot_duration_mins, business_name")
            .eq("id", tenant_id)
            .single()
            .execute()
        )
        return result.data if result.data else None
    except Exception as e:
        logger.error("[availability] tenant config fetch failed: %s", e)
        return None


async def fetch_scheduling_data(deps: dict) -> dict | None:
    """Return {appointments, calendar_events, service_zones, zone_travel_buffers,
    calendar_blocks} from the prefetched slot_cache when fresh; otherwise
    live-fetch in parallel and refresh the cache. Returns None on fetch failure.

    Shared by all three availability tools so cache invalidation
    (book_appointment clears deps["_slot_cache"]) affects all of them.
    """
    tenant_id = deps.get("tenant_id")
    supabase = deps["supabase"]
    if not tenant_id:
        return None

    cache = deps.get("_slot_cache")
    if cache and (_time.time() - cache.get("fetched_at", 0)) < SLOT_CACHE_TTL_S:
        logger.info(
            "[availability] slot_cache hit age=%.1fs appts=%d events=%d zones=%d buffers=%d blocks=%d",
            _time.time() - cache.get("fetched_at", 0),
            len(cache.get("appointments") or []),
            len(cache.get("calendar_events") or []),
            len(cache.get("service_zones") or []),
            len(cache.get("zone_travel_buffers") or []),
            len(cache.get("calendar_blocks") or []),
        )
        return {
            "appointments": cache.get("appointments") or [],
            "calendar_events": cache.get("calendar_events") or [],
            "service_zones": cache.get("service_zones") or [],
            "zone_travel_buffers": cache.get("zone_travel_buffers") or [],
            "calendar_blocks": cache.get("calendar_blocks") or [],
        }

    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        appts_r, events_r, zones_r, buffers_r, blocks_r = await asyncio.gather(
            asyncio.to_thread(
                lambda: supabase.table("appointments")
                .select("start_time, end_time, zone_id")
                .eq("tenant_id", tenant_id)
                .neq("status", "cancelled")
                .neq("status", "completed")
                .gte("end_time", now_iso)
                .execute()
            ),
            asyncio.to_thread(
                lambda: supabase.table("calendar_events")
                .select("start_time, end_time")
                .eq("tenant_id", tenant_id)
                .gte("end_time", now_iso)
                .execute()
            ),
            asyncio.to_thread(
                lambda: supabase.table("service_zones")
                .select("id, name, postal_codes")
                .eq("tenant_id", tenant_id)
                .execute()
            ),
            asyncio.to_thread(
                lambda: supabase.table("zone_travel_buffers")
                .select("zone_a_id, zone_b_id, buffer_mins")
                .eq("tenant_id", tenant_id)
                .execute()
            ),
            asyncio.to_thread(
                lambda: supabase.table("calendar_blocks")
                .select("start_time, end_time")
                .eq("tenant_id", tenant_id)
                .gte("end_time", now_iso)
                .execute()
            ),
        )
    except Exception as e:
        logger.error("[availability] scheduling data fetch failed: %s", e)
        return None

    sched = {
        "appointments": appts_r.data or [],
        "calendar_events": events_r.data or [],
        "service_zones": zones_r.data or [],
        "zone_travel_buffers": buffers_r.data or [],
        "calendar_blocks": blocks_r.data or [],
    }
    deps["_slot_cache"] = {"fetched_at": _time.time(), **sched}
    return sched


# ─────────────────────────────────────────────────────────────────────────────
# Slot math
# ─────────────────────────────────────────────────────────────────────────────

def calc_slots_for_dates(
    tenant: dict,
    dates: list[str],
    sched: dict,
    tenant_timezone: str,
) -> list[dict]:
    """Flatten calculate_available_slots() across one or more dates."""
    slot_duration = tenant.get("slot_duration_mins") or 60
    all_slots: list[dict] = []
    for date_str in dates:
        day_slots = calculate_available_slots(
            working_hours=tenant.get("working_hours") or {},
            slot_duration_mins=slot_duration,
            existing_bookings=sched["appointments"],
            external_blocks=sched["calendar_events"] + sched["calendar_blocks"],
            zones=sched["service_zones"],
            zone_pair_buffers=format_zone_pair_buffers(sched["zone_travel_buffers"]),
            target_date=date_str,
            tenant_timezone=tenant_timezone,
            max_slots=50,
        )
        all_slots.extend(day_slots)
    return all_slots


def next_n_local_dates(n: int, tenant_timezone: str) -> list[str]:
    """Return the next N calendar dates (including today) as YYYY-MM-DD strings
    in the tenant's local timezone."""
    now = datetime.now(timezone.utc)
    return [to_local_date_string(now + timedelta(days=i), tenant_timezone) for i in range(n)]


def tenant_today(tenant_timezone: str) -> str:
    """Today's date (YYYY-MM-DD) in tenant local time."""
    return to_local_date_string(datetime.now(timezone.utc), tenant_timezone)


# ─────────────────────────────────────────────────────────────────────────────
# Tool-call log (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def log_tool_call(deps: dict, entry: dict[str, Any]) -> None:
    """Append one entry to deps['_tool_call_log']. Used by post-call pipeline
    for silent hallucination detection. ts is added if not provided."""
    entry.setdefault("ts", datetime.now(timezone.utc).isoformat())
    deps.setdefault("_tool_call_log", []).append(entry)
