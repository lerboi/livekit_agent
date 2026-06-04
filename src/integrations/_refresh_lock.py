"""Shared OAuth refresh-lock client for the LiveKit agent integrations.

Python port of the lease-based concurrency guard in the Next.js adapter
(`homeservice_agent/src/lib/integrations/adapter.js` :: refreshTokenIfNeeded).

Serializes concurrent OAuth refreshes per (tenant_id, provider) so the agent
and the dashboard cannot both fire an HTTP refresh at once. This matters most
for Jobber, whose refresh-token rotation is single-use: a second concurrent
refresh either 401s or orphans the first caller's rotated token.

Mirrored RPC contract (supabase/migrations/058_oauth_refresh_locks.sql):
  - try_acquire_oauth_refresh_lock(p_tenant_id UUID, p_provider TEXT,
        p_ttl_ms INT DEFAULT 30000) RETURNS UUID
        → new holder UUID if won, NULL if another non-expired lock is held.
  - release_oauth_refresh_lock(p_tenant_id UUID, p_provider TEXT,
        p_holder_id UUID) RETURNS VOID
        → deletes the row only if holder_id matches (stale release = no-op).

Timing constants match the JS adapter exactly:
  REFRESH_LOCK_TTL_MS = 30_000, WAIT_MS = 3_000, POLL_MS = 200.

Fail-soft: every helper swallows exceptions and degrades to the un-serialized
path (availability beats perfect dedup), matching the JS `lockErr` fall-through.
Nothing here ever raises into the live-call hot path.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from ..supabase_client import get_supabase_admin

logger = logging.getLogger(__name__)

REFRESH_LOCK_TTL_MS = 30_000
REFRESH_LOCK_WAIT_MS = 3_000
REFRESH_LOCK_POLL_MS = 200


async def acquire_refresh_lock(tenant_id: str, provider: str) -> Optional[str]:
    """Try to acquire the lease lock for (tenant_id, provider).

    Returns the holder UUID (string) if this caller won the slot, or None if
    another caller currently holds a non-expired lease. On RPC error returns
    None as well — callers treat both "lost" and "lock unavailable" as the
    contested branch; the JS adapter's `lockErr` fall-through and the poll
    timeout both ultimately refresh anyway, so a None never deadlocks.
    """

    def _rpc():
        admin = get_supabase_admin()
        resp = admin.rpc(
            "try_acquire_oauth_refresh_lock",
            {
                "p_tenant_id": tenant_id,
                "p_provider": provider,
                "p_ttl_ms": REFRESH_LOCK_TTL_MS,
            },
        ).execute()
        return getattr(resp, "data", None)

    try:
        holder = await asyncio.to_thread(_rpc)
    except Exception as exc:  # noqa: BLE001
        # Lock table/RPC unavailable — fall through to the un-serialized path.
        logger.warning(
            "refresh_lock: acquire RPC failed for %s/%s: %s",
            provider, tenant_id, exc,
        )
        return None
    if isinstance(holder, str) and holder:
        return holder
    return None


async def release_refresh_lock(tenant_id: str, provider: str, holder_id: str) -> None:
    """Release the lease lock. Best-effort; the 30s TTL is the backstop."""
    if not holder_id:
        return

    def _rpc():
        admin = get_supabase_admin()
        admin.rpc(
            "release_oauth_refresh_lock",
            {
                "p_tenant_id": tenant_id,
                "p_provider": provider,
                "p_holder_id": holder_id,
            },
        ).execute()

    try:
        await asyncio.to_thread(_rpc)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "refresh_lock: release RPC failed for %s/%s: %s",
            provider, tenant_id, exc,
        )


async def poll_for_fresh_credential(
    cred_id: str,
    *,
    buffer_seconds: float,
    expiry_to_epoch: Callable[[object], float],
) -> Optional[dict]:
    """Poll accounting_credentials for the lock winner's freshly-persisted row.

    Mirrors the JS loser branch: wait up to REFRESH_LOCK_WAIT_MS, polling every
    REFRESH_LOCK_POLL_MS, returning the row once its expiry is comfortably in
    the future (> now + buffer_seconds, matching the winner's refresh buffer).
    Returns None on timeout so the caller falls back to refreshing itself.
    """
    import time as _time

    deadline = _time.monotonic() + (REFRESH_LOCK_WAIT_MS / 1000.0)

    def _read():
        admin = get_supabase_admin()
        resp = (
            admin.table("accounting_credentials")
            .select("*")
            .eq("id", cred_id)
            .maybe_single()
            .execute()
        )
        return getattr(resp, "data", None)

    while _time.monotonic() < deadline:
        await asyncio.sleep(REFRESH_LOCK_POLL_MS / 1000.0)
        try:
            fresh = await asyncio.to_thread(_read)
        except Exception as exc:  # noqa: BLE001
            logger.warning("refresh_lock: poll read failed for cred=%s: %s", cred_id, exc)
            continue
        if not fresh:
            continue
        expiry_epoch = expiry_to_epoch(fresh.get("expiry_date"))
        # expiry_epoch is wall-clock epoch seconds; compare to wall-clock now.
        if expiry_epoch and (expiry_epoch - _time.time()) > buffer_seconds:
            return fresh
    return None
