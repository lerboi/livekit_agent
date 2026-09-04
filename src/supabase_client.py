import logging
import os

import httpx
from supabase import create_client, Client
from supabase.lib.client_options import SyncClientOptions

logger = logging.getLogger(__name__)

_supabase: Client | None = None

# P1.10 (2026-09-04): keep-alive HTTP client for Supabase. Supabase lives in
# ap-northeast-1 (Tokyo, ~130 ms RTT from the US worker) and httpx's default
# keepalive_expiry is 5 s, so between tool calls the pooled connection expired
# and the next query paid a fresh TLS handshake (~250-350 ms) on top of the
# RTT. This client (a) never expires idle keep-alive connections, (b) keeps
# HTTP/2 (postgrest's own default), and (c) caps a single request at 10 s
# (postgrest's default is 120 s — nothing the agent does legitimately runs
# that long, and a hung query must not hold a call for two minutes).
# agent.prewarm() issues one tiny query per idle job process so the TLS
# session is already open before a call arrives. `false` restores the plain
# create_client() path without a deploy.
SUPABASE_KEEPALIVE = (
    os.environ.get("VOCO_SUPABASE_KEEPALIVE", "true").strip().lower() != "false"
)
SUPABASE_REQUEST_TIMEOUT_S = float(os.environ.get("VOCO_SUPABASE_TIMEOUT_S", "10"))


def _build_keepalive_client(url: str, key: str) -> Client:
    http = httpx.Client(
        http2=True,
        timeout=httpx.Timeout(SUPABASE_REQUEST_TIMEOUT_S),
        limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=None),
    )
    return create_client(url, key, options=SyncClientOptions(httpx_client=http))


def get_supabase_admin() -> Client:
    """Get a Supabase service-role client (bypasses RLS).
    Same pattern as the Next.js app's src/lib/supabase.js.

    Lazily built module global — one per process (LiveKit job processes are
    spawned/forkserver'd with fresh module state, so each builds its own).
    """
    global _supabase
    if _supabase is None:
        url = os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise RuntimeError("Missing NEXT_PUBLIC_SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
        if SUPABASE_KEEPALIVE:
            try:
                _supabase = _build_keepalive_client(url, key)
            except Exception as exc:  # noqa: BLE001 — fail open to today's client
                logger.warning(
                    "[supabase] keep-alive client construction failed (%s) — "
                    "falling back to default create_client()", exc,
                )
                _supabase = create_client(url, key)
        else:
            _supabase = create_client(url, key)
    return _supabase


def warm_supabase_connection() -> bool:
    """Open the TLS session to Supabase ahead of the first real query (called
    from agent.prewarm in every idle job process). Never raises."""
    try:
        get_supabase_admin().table("tenants").select("id").limit(1).execute()
        return True
    except Exception as exc:  # noqa: BLE001 — prewarm must never fail the process
        logger.warning("[supabase] prewarm ping failed: %s", exc)
        return False
