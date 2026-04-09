"""Soft cap enforcement for owner-pickup routing.

Phase 39 ships this as a contract-only deliverable: it is NOT yet called from
the webhook handler path (Plan 39-05's /twilio/incoming-call always returns
the hardcoded AI TwiML branch). Phase 40 wires it into the live routing
decision per the D-11 composition pattern:

    decision = evaluate_schedule(schedule, tz, now_utc)
    if decision.mode == 'owner_pickup':
        if not await check_outbound_cap(tenant_id, country):
            decision = ScheduleDecision(mode='ai', reason='soft_cap_hit')

Query:
    SELECT COALESCE(SUM(outbound_dial_duration_sec), 0)
    FROM calls
    WHERE tenant_id = $1
      AND created_at >= date_trunc('month', now())

UTC anchoring is acceptable at current scale (see RESEARCH.md §5 "UTC Anchoring"):
the monthly cap is anchored to UTC midnight on the 1st, so tenants near the UTC
month boundary see up to 8 hours of pre-month-transition calls excluded. Revisit
only if a material amount of traffic lives in that window.

Limits (D-17):
    US = 5000 minutes = 300000 seconds
    CA = 5000 minutes = 300000 seconds
    SG = 2500 minutes = 150000 seconds
    Unknown country -> falls back to US limit (fail-open safe default)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger("voco-webhook")


# Limits in seconds (D-17)
_LIMITS_SEC: dict[str, int] = {
    "US": 5000 * 60,   # 300_000
    "CA": 5000 * 60,   # 300_000
    "SG": 2500 * 60,   # 150_000
}
_DEFAULT_LIMIT_SEC: int = 5000 * 60  # fail-open: unknown country -> US limit


def _month_start_utc_iso(now: datetime | None = None) -> str:
    """Return ISO8601 string for 00:00:00 UTC on the 1st of the current month.

    Kept as a module-level helper so tests can monkey-patch it for deterministic
    month boundaries without mocking datetime.now.
    """
    now = now or datetime.now(tz=timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()


async def check_outbound_cap(tenant_id: str, country: str) -> bool:
    """Return True if the tenant is under the monthly outbound cap, False if at/over.

    Args:
        tenant_id: tenant UUID string
        country: ISO-3166 alpha-2 country code (e.g. 'US', 'CA', 'SG'); case-insensitive

    Returns:
        True if sum(outbound_dial_duration_sec) this month < country limit, else False
    """
    # Lazy import so tests can mock src.supabase_client before this function is called
    from src.supabase_client import get_supabase_admin

    limit_sec = _LIMITS_SEC.get(country.upper(), _DEFAULT_LIMIT_SEC)
    month_start = _month_start_utc_iso()

    supabase = get_supabase_admin()
    response = await asyncio.to_thread(
        lambda: supabase.table("calls")
        .select("outbound_dial_duration_sec")
        .eq("tenant_id", tenant_id)
        .gte("created_at", month_start)
        .execute()
    )

    rows = response.data or []
    total_sec = sum(int(r.get("outbound_dial_duration_sec") or 0) for r in rows)

    under_cap = total_sec < limit_sec
    if not under_cap:
        logger.warning(
            "[webhook] Outbound cap hit: tenant=%s country=%s total=%ds limit=%ds",
            tenant_id, country, total_sec, limit_sec,
        )
    return under_cap
