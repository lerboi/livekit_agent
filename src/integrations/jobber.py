"""Jobber read-side integration for the LiveKit agent (Phase 56 Plan 05).

Python counterpart to src/lib/integrations/jobber.js in the Voco monorepo.
Mirrors the return shape field-for-field so the merge helper in Plan 06 can
union Jobber + Xero uniformly.

Key constraints (research Pitfalls 3, 7, 10):
- X-JOBBER-GRAPHQL-VERSION header REQUIRED on every request (400 otherwise).
- Refresh-token rotation is MANDATORY — every refresh returns a new
  refresh_token which MUST be persisted immediately; re-using the old
  refresh_token fails silently.
- httpx.Timeout(connect=0.3, read=0.7) — socket-level self-terminate so
  the 800ms race in Plan 06 cannot leak in-flight connections.

Never raises, never logs token material. On any failure, returns None.

Caller is responsible for resolving tenant_id server-side (never from a
request body / tool argument). Service-role Supabase bypasses RLS; tenant_id
is the only isolation primitive.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

# Imported at module level (not lazily) so Phase 58 telemetry can resolve the
# admin client at call time AND tests can patch `jobber_mod.get_supabase_admin`.
from ..supabase_client import get_supabase_admin
from ..lib.telemetry import emit_integration_fetch
from ._refresh_lock import (
    acquire_refresh_lock,
    poll_for_fresh_credential,
    release_refresh_lock,
)

logger = logging.getLogger(__name__)

JOBBER_GRAPHQL_URL = "https://api.getjobber.com/api/graphql"
JOBBER_TOKEN_URL = "https://api.getjobber.com/api/oauth/token"
JOBBER_API_VERSION = "2025-04-16"  # keep in sync with Next.js Plan 01
DEFAULT_PHONE_REGION = "US"
E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")

# Token refresh gets its OWN generous HTTP budget, separate from the
# sub-second context-fetch client. Jobber consumes the single-use refresh
# token the moment its server processes the request — aborting before the
# response is read orphans the rotated token and permanently bricks the
# connection (owner must reconnect). Never let the 0.3/0.7s fetch timeouts
# or the caller's 0.8s context budget cut a rotation short.
REFRESH_HTTP_TIMEOUT = httpx.Timeout(connect=3.0, read=10.0, write=3.0, pool=3.0)

# Strong refs to in-flight shielded refresh tasks — asyncio holds only weak
# refs to tasks, and these must outlive a cancelled context fetch.
_REFRESH_TASKS: set = set()

OUTSTANDING_STATUSES = {"AWAITING_PAYMENT", "BAD_DEBT", "PARTIAL", "PAST_DUE"}

# Mirrors src/lib/integrations/jobber.js FETCH_QUERY literal.
FETCH_QUERY = """
query FetchClientByPhone($phone: String!) {
  clients(first: 25, filter: { phoneNumber: $phone }) {
    nodes {
      id
      name
      emails { address }
      phones { number }
      jobs(first: 4, sort: [{ key: UPDATED_AT, direction: DESCENDING }]) {
        nodes {
          jobNumber
          title
          jobStatus
          startAt
          endAt
          visits(first: 1, filter: { status: UPCOMING }) { nodes { startAt } }
        }
      }
      invoices(first: 10) {
        nodes { invoiceNumber issuedDate amount amountOutstanding invoiceStatus }
      }
      visits(first: 1, sort: [{ key: COMPLETED_AT, direction: DESCENDING }], filter: { completed: true }) {
        nodes { endAt completedAt }
      }
    }
  }
}
"""


# ---- Phone normalization --------------------------------------------------


def _normalize_free_form(raw: str, default_region: str = DEFAULT_PHONE_REGION) -> Optional[str]:
    """Normalize a free-form phone (e.g. '(555) 123-4567') to E.164.

    src/lib/phone.py:_normalize_phone only handles SIP-attribute strings
    (`sip:+15551234567@...`) — it does NOT parse US free-form formats that
    Jobber stores. We use `phonenumbers` here to match the Next.js side's
    libphonenumber-js behavior.
    """
    if not raw:
        return None
    try:
        import phonenumbers

        parsed = phonenumbers.parse(raw, default_region)
        if not phonenumbers.is_possible_number(parsed):
            return None
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except Exception:  # noqa: BLE001
        return None


# ---- Token / credential helpers (patchable from tests) --------------------


def _decode_jwt_exp_ms(jwt: str) -> Optional[int]:
    """Jobber access-token is a JWT; decode `exp` claim to ms-since-epoch."""
    try:
        parts = jwt.split(".")
        if len(parts) < 2:
            return None
        pad = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
        exp = payload.get("exp")
        return int(exp) * 1000 if isinstance(exp, (int, float)) else None
    except Exception:  # noqa: BLE001
        return None


def _expiry_to_epoch(expiry_value) -> float:
    """Parse accounting_credentials.expiry_date to epoch seconds."""
    if expiry_value is None:
        return 0.0
    if isinstance(expiry_value, (int, float)):
        return float(expiry_value) / 1000.0
    s = str(expiry_value).strip()
    if not s:
        return 0.0
    if s.lstrip("-").isdigit():
        return float(s) / 1000.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


async def _load_credentials(tenant_id: str) -> Optional[dict]:
    """Service-role read of accounting_credentials for (tenant_id, provider='jobber')."""

    def _query() -> Optional[dict]:
        admin = get_supabase_admin()
        resp = (
            admin.table("accounting_credentials")
            .select("*")
            .eq("tenant_id", tenant_id)
            .eq("provider", "jobber")
            .maybe_single()
            .execute()
        )
        return resp.data if resp and getattr(resp, "data", None) else None

    try:
        return await asyncio.to_thread(_query)
    except Exception as exc:  # noqa: BLE001
        logger.warning("jobber: failed to load credentials for tenant=%s: %s", tenant_id, exc)
        return None


async def _persist_refreshed_tokens(
    cred_id: str,
    access_token: str,
    refresh_token: str,
    expiry_date_ms: Optional[int],
) -> None:
    """Atomic UPDATE: write BOTH access_token AND refresh_token AND expiry_date.

    Critical — Jobber rotates refresh_token on every refresh (Pitfall 3).
    Losing the new one = auth break on the next refresh cycle.

    expiry_date is the BIGINT epoch-MILLISECONDS column (migration 030); the
    Next.js writer stores epoch-ms. Writing an ISO string here would be
    rejected by Postgres (text→bigint, 22P02) and the bare except below would
    swallow it, so agent-side refreshes never persist.
    """
    def _update() -> None:
        admin = get_supabase_admin()
        (
            admin.table("accounting_credentials")
            .update(
                {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expiry_date": expiry_date_ms,
                    "error_state": None,  # heal on success
                }
            )
            .eq("id", cred_id)
            .execute()
        )

    try:
        await asyncio.to_thread(_update)
    except Exception as exc:  # noqa: BLE001
        # Intentionally NOT logging exc chain — may contain response body.
        logger.warning("jobber: failed to persist refreshed tokens for cred=%s", cred_id)


async def _persist_refresh_failure(cred_id: str) -> None:
    """Mark credential row error_state='token_refresh_failed'. Silent on failure."""
    def _update() -> None:
        admin = get_supabase_admin()
        (
            admin.table("accounting_credentials")
            .update({"error_state": "token_refresh_failed"})
            .eq("id", cred_id)
            .execute()
        )

    try:
        await asyncio.to_thread(_update)
    except Exception:  # noqa: BLE001
        pass


async def _touch_last_context_fetch_at(cred_id: str) -> None:
    """Telemetry seed — best-effort UPDATE; silent on failure."""
    def _update() -> None:
        admin = get_supabase_admin()
        (
            admin.table("accounting_credentials")
            .update({"last_context_fetch_at": datetime.now(timezone.utc).isoformat()})
            .eq("id", cred_id)
            .execute()
        )

    try:
        await asyncio.to_thread(_update)
    except Exception:  # noqa: BLE001
        pass


async def _do_wire_refresh(cred: dict) -> Optional[dict]:
    """POST refresh_token grant; persist the NEW token set back. Caller holds lock.

    Uses a DEDICATED httpx client with REFRESH_HTTP_TIMEOUT — never the
    sub-second context-fetch client (a 0.7s read timeout on the token
    endpoint loses the rotated refresh_token Jobber has already issued).

    error_state='token_refresh_failed' is persisted ONLY on definitive
    grant rejections (HTTP 400/401 — revoked/expired/consumed refresh
    token). Timeouts, network errors, and 5xx are transient: the stored
    refresh token is still good, so flagging them would show a false
    "Reconnect Jobber" banner and email the owner for nothing.

    Returns an updated cred dict with fresh access_token + rotated refresh_token
    on success. Returns None on any failure.
    """
    client_id = os.environ.get("JOBBER_CLIENT_ID", "")
    client_secret = os.environ.get("JOBBER_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        # Deployment config problem — reconnecting wouldn't fix it; don't flag.
        logger.warning("jobber: JOBBER_CLIENT_ID/SECRET missing; cannot refresh")
        return None

    try:
        async with httpx.AsyncClient(timeout=REFRESH_HTTP_TIMEOUT) as client:
            resp = await client.post(
                JOBBER_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": cred["refresh_token"],
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
    except Exception as exc:  # noqa: BLE001
        # Transient (timeout / connection error) — log the exception TYPE only
        # (never the message, may echo body) and leave error_state untouched.
        logger.warning("jobber: refresh exception type=%s", type(exc).__name__)
        return None

    status = getattr(resp, "status_code", 500)
    if status != 200:
        logger.warning("jobber: refresh non-200 status=%d", status)
        if status in (400, 401):
            # invalid_grant / unauthorized — the refresh token is dead.
            await _persist_refresh_failure(cred["id"])
        return None

    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        logger.warning("jobber: refresh 200 with unparseable body")
        return None

    new_access = body.get("access_token")
    new_refresh = body.get("refresh_token")
    # Jobber mandates rotation — a missing new refresh_token is a contract
    # violation. Do NOT persist the old token (would break next cycle).
    if not new_access or not new_refresh:
        logger.warning("jobber: refresh 200 missing token rotation fields")
        return None

    # expiry_date is BIGINT epoch-MILLISECONDS (migration 030). _decode_jwt_exp_ms
    # already yields epoch-ms — persist it directly; do NOT convert to ISO (would
    # 22P02 on the bigint column and never persist, breaking the next cycle).
    expiry_ms = _decode_jwt_exp_ms(new_access)

    await _persist_refreshed_tokens(cred["id"], new_access, new_refresh, expiry_ms)

    return {
        **cred,
        "access_token": new_access,
        "refresh_token": new_refresh,
        "expiry_date": expiry_ms,
        "error_state": None,
    }


async def _refresh_token_locked(cred: dict) -> Optional[dict]:
    """Refresh with the per-(tenant, provider) lease lock (mirrors adapter.js).

    Acquire the lock before the wire refresh. The winner refreshes + persists +
    releases; a loser polls the DB for the winner's freshly-persisted token and
    reuses it. On poll timeout the loser refreshes itself (logged). Critical for
    Jobber's single-use refresh-token rotation: two concurrent refreshers would
    otherwise orphan one caller's rotated token.
    """
    tenant_id = cred.get("tenant_id")
    holder_id = None
    if tenant_id:
        holder_id = await acquire_refresh_lock(tenant_id, "jobber")
        if holder_id is None:
            fresh = await poll_for_fresh_credential(
                cred["id"],
                # Jobber refreshes on hard expiry (no buffer); accept any
                # not-yet-expired persisted token from the winner.
                buffer_seconds=0.0,
                expiry_to_epoch=_expiry_to_epoch,
            )
            if fresh:
                return fresh
            logger.warning(
                "jobber: refresh lock contested for tenant=%s; poll timed out, "
                "refreshing anyway", tenant_id,
            )

    try:
        return await _do_wire_refresh(cred)
    finally:
        if holder_id and tenant_id:
            await release_refresh_lock(tenant_id, "jobber", holder_id)


async def _refresh_token(cred: dict) -> Optional[dict]:
    """Cancellation-shielded refresh entry point.

    The pre-session context fetch runs under asyncio.wait_for(0.8s). Without a
    shield, that deadline CANCELS the refresh mid-POST — Jobber has already
    consumed the single-use refresh token server-side, the rotated replacement
    is never read or persisted, and the stored token is permanently dead. The
    shield lets the outer fetch give up (call proceeds without context) while
    the rotation runs to completion in the background and persists, so the
    NEXT call finds a healthy token.
    """
    task = asyncio.ensure_future(_refresh_token_locked(cred))
    _REFRESH_TASKS.add(task)
    task.add_done_callback(_REFRESH_TASKS.discard)
    return await asyncio.shield(task)


# ---- GraphQL request ------------------------------------------------------


def _graphql_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "X-JOBBER-GRAPHQL-VERSION": JOBBER_API_VERSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def _post_graphql(
    client: httpx.AsyncClient,
    access_token: str,
    phone_e164: str,
) -> Optional[httpx.Response]:
    try:
        return await client.post(
            JOBBER_GRAPHQL_URL,
            json={"query": FETCH_QUERY, "variables": {"phone": phone_e164}},
            headers=_graphql_headers(access_token),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("jobber: graphql exception type=%s", type(exc).__name__)
        return None


def _match_phone(phones: list, target_e164: str) -> bool:
    for p in phones or []:
        normalized = _normalize_free_form(p.get("number", ""))
        if normalized and normalized == target_e164:
            return True
    return False


def _shape_response(matched: dict) -> dict:
    """Mirror Plan 01 Next.js return shape field-for-field (camelCase keys)."""
    now = datetime.now(timezone.utc)

    jobs_raw = (matched.get("jobs") or {}).get("nodes") or []
    jobs = []
    for j in jobs_raw:
        next_visit = None
        vn = (j.get("visits") or {}).get("nodes") or []
        if vn:
            next_visit = vn[0].get("startAt")
        jobs.append({
            "jobNumber": j.get("jobNumber"),
            "title": j.get("title"),
            "status": j.get("jobStatus"),
            "startAt": j.get("startAt"),
            "endAt": j.get("endAt"),
            "nextVisitDate": next_visit,
        })

    def _future(job):
        nv = job.get("nextVisitDate")
        if not nv:
            return False
        try:
            return datetime.fromisoformat(str(nv).replace("Z", "+00:00")) >= now
        except Exception:  # noqa: BLE001
            return False

    # Future visits ASC first, then remainder in GraphQL order (UPDATED_AT DESC).
    jobs.sort(key=lambda j: (not _future(j), j.get("nextVisitDate") or ""))
    jobs = jobs[:4]

    invoice_nodes = (matched.get("invoices") or {}).get("nodes") or []
    outstanding = [inv for inv in invoice_nodes if inv.get("invoiceStatus") in OUTSTANDING_STATUSES]
    outstanding_balance = float(sum((inv.get("amountOutstanding") or 0) for inv in outstanding))
    outstanding_invoices = [{
        "invoiceNumber": inv.get("invoiceNumber"),
        "issuedAt": inv.get("issuedDate"),
        "amount": float(inv.get("amount") or 0),
        "amountOutstanding": float(inv.get("amountOutstanding") or 0),
        "status": inv.get("invoiceStatus"),
    } for inv in outstanding[:3]]

    vs = (matched.get("visits") or {}).get("nodes") or []
    last_visit_date = vs[0].get("endAt") if vs else None

    return {
        "client": {
            "id": matched.get("id"),
            "name": matched.get("name"),
            "email": (matched.get("emails") or [{}])[0].get("address") if matched.get("emails") else None,
        },
        "recentJobs": jobs,
        "outstandingInvoices": outstanding_invoices,
        "outstandingBalance": outstanding_balance,
        "lastVisitDate": last_visit_date,
    }


# ---- Public entry ---------------------------------------------------------


async def fetch_jobber_customer_by_phone(
    tenant_id: str,
    phone_e164: str,
) -> Optional[dict]:
    """Fetch Jobber customer context by phone.

    Mirrors src/lib/integrations/jobber.js :: fetchJobberCustomerByPhone.

    Args:
        tenant_id: Voco tenant_id (UUID), resolved server-side by caller.
        phone_e164: Caller phone in E.164 (e.g. "+15551234567").

    Returns:
        dict with keys {client, recentJobs, outstandingInvoices,
        outstandingBalance, lastVisitDate} or None on no-match /
        disconnected / timeout / any error. Never raises.
    """
    try:
        if not isinstance(tenant_id, str) or not tenant_id:
            return None
        if not isinstance(phone_e164, str) or not phone_e164:
            return None
        if not E164_RE.match(phone_e164):
            return None

        # Phase 58 CTX-01: per-fetch latency measurement. `_cache_hit` is
        # always False (no in-agent cache layer today). See xero.py for the
        # symmetric note.
        _fetch_start = time.perf_counter()
        _cache_hit = False

        cred = await _load_credentials(tenant_id)
        if not cred:
            return None

        # Socket-level self-terminate (Pitfall 10). Combined with Plan 06's
        # asyncio.wait_for this is belt-and-suspenders. All four phases must
        # be set explicitly (httpx rejects partial specification).
        timeout = httpx.Timeout(connect=0.3, read=0.7, write=0.3, pool=0.3)

        async with httpx.AsyncClient(timeout=timeout) as client:
            # Proactive refresh if expired — runs on its own shielded task +
            # dedicated long-timeout client (see _refresh_token), NOT this
            # sub-second fetch client.
            expiry_epoch = _expiry_to_epoch(cred.get("expiry_date"))
            if expiry_epoch and expiry_epoch <= datetime.now(timezone.utc).timestamp():
                refreshed = await _refresh_token(cred)
                if not refreshed:
                    return None
                cred = refreshed

            resp = await _post_graphql(client, cred["access_token"], phone_e164)

            # Reactive refresh + single retry on 401
            if resp is not None and getattr(resp, "status_code", 500) == 401:
                refreshed = await _refresh_token(cred)
                if not refreshed:
                    return None
                cred = refreshed
                resp = await _post_graphql(client, cred["access_token"], phone_e164)

            if resp is None or getattr(resp, "status_code", 500) != 200:
                return None

            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                return None

            if body.get("errors"):
                return None

            nodes = ((body.get("data") or {}).get("clients") or {}).get("nodes") or []
            matched = next(
                (c for c in nodes if _match_phone(c.get("phones") or [], phone_e164)),
                None,
            )
            if not matched:
                return None

            shaped = _shape_response(matched)

        # Phase 58 CTX-01: parallelize last_context_fetch_at UPDATE with the
        # integration_fetch activity_log INSERT via asyncio.gather (outside
        # the httpx client context). Both writes are silent-on-failure.
        # Fall back to touch-only if admin client unavailable (test harness).
        _duration_ms = int((time.perf_counter() - _fetch_start) * 1000)
        _counts = {
            "customers": 1 if shaped.get("client") else 0,
            "jobs": len(shaped.get("recentJobs") or []),
            "invoices": len(shaped.get("outstandingInvoices") or []),
        }
        try:
            admin = get_supabase_admin()
        except Exception as exc:  # noqa: BLE001
            logger.warning("jobber: telemetry skipped — admin client unavailable: %s", exc)
            admin = None

        if admin is not None:
            await asyncio.gather(
                _touch_last_context_fetch_at(cred["id"]),
                emit_integration_fetch(
                    admin,
                    tenant_id=tenant_id,
                    provider="jobber",
                    duration_ms=_duration_ms,
                    cache_hit=_cache_hit,
                    counts=_counts,
                    phone_e164=phone_e164,
                ),
            )
        else:
            await _touch_last_context_fetch_at(cred["id"])
        return shaped
    except Exception:  # noqa: BLE001
        # Never raise — Plan 06's 800ms race silently skips on None.
        return None
