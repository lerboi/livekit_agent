"""Phase 58 CTX-01 — shared helpers for integration telemetry writes to activity_log.

Uses the REAL activity_log column names (event_type + metadata) per migration 004,
NOT the CONTEXT D-06 wording (action + meta). Documented in 58-RESEARCH §B.2.

Every helper is silent-on-failure: telemetry errors must never bubble up and
break the call path. Callers should fire these inside asyncio.gather (per-fetch)
or asyncio.create_task (fanout, fire-and-forget) so they don't serialize with
the primary return path.

Helper API (admin client injected by caller, so tests that patch the caller's
module-level `get_supabase_admin` see the mock):

    await emit_integration_fetch(admin, tenant_id, provider, duration_ms,
                                 cache_hit, counts, phone_e164)

    await emit_integration_fetch_fanout(admin, tenant_id, duration_ms,
                                        per_task_ms, call_id)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)


async def emit_integration_fetch(
    admin: Any,
    tenant_id: str,
    provider: str,
    duration_ms: int,
    cache_hit: bool,
    counts: Mapping[str, int],
    phone_e164: Optional[str],
) -> None:
    """Log one activity_log row for a single fetchCustomerByPhone call.

    Silent-on-failure: any exception is logged at WARNING and swallowed so the
    primary call path is never broken by telemetry.
    """

    def _insert() -> None:
        try:
            (
                admin.table("activity_log")
                .insert(
                    {
                        "tenant_id": tenant_id,
                        "event_type": "integration_fetch",
                        "metadata": {
                            "provider": provider,
                            "duration_ms": int(duration_ms),
                            "cache_hit": bool(cache_hit),
                            "counts": dict(counts),
                            "phone_e164": phone_e164,
                        },
                    }
                )
                .execute()
            )
        except Exception as exc:  # noqa: BLE001 — telemetry must not propagate
            logger.warning("emit_integration_fetch failed: %s", exc)

    try:
        await asyncio.to_thread(_insert)
    except Exception as exc:  # noqa: BLE001 — belt-and-suspenders
        logger.warning("emit_integration_fetch to_thread failed: %s", exc)


async def emit_integration_fetch_fanout(
    admin: Any,
    tenant_id: str,
    duration_ms: int,
    per_task_ms: Mapping[str, int],
    call_id: Optional[str],
) -> None:
    """Log one activity_log row for the pre-session asyncio.gather boundary.

    Fired fire-and-forget via asyncio.create_task at the call site so the
    primary call path never waits for this write.
    """

    def _insert() -> None:
        try:
            (
                admin.table("activity_log")
                .insert(
                    {
                        "tenant_id": tenant_id,
                        "event_type": "integration_fetch_fanout",
                        "metadata": {
                            "duration_ms": int(duration_ms),
                            "per_task_ms": {k: int(v) for k, v in per_task_ms.items()},
                            "call_id": call_id,
                        },
                    }
                )
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("emit_integration_fetch_fanout failed: %s", exc)

    try:
        await asyncio.to_thread(_insert)
    except Exception as exc:  # noqa: BLE001
        logger.warning("emit_integration_fetch_fanout to_thread failed: %s", exc)
