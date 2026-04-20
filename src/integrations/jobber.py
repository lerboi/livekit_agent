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

logger = logging.getLogger(__name__)

JOBBER_GRAPHQL_URL = "https://api.getjobber.com/api/graphql"
JOBBER_TOKEN_URL = "https://api.getjobber.com/api/oauth/token"
JOBBER_API_VERSION = "2025-04-16"  # keep in sync with Next.js Plan 01
DEFAULT_PHONE_REGION = "US"
E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")

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
    expiry_date_iso: Optional[str],
) -> None:
    """Atomic UPDATE: write BOTH access_token AND refresh_token AND expiry_date.

    Critical — Jobber rotates refresh_token on every refresh (Pitfall 3).
    Losing the new one = auth break on the next refresh cycle.
    """
    def _update() -> None:
        admin = get_supabase_admin()
        (
            admin.table("accounting_credentials")
            .update(
                {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expiry_date": expiry_date_iso,
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


async def _refresh_token(client: httpx.AsyncClient, cred: dict) -> Optional[dict]:
    """POST refresh_token grant; persist the NEW token set back.

    Returns an updated cred dict with fresh access_token + rotated refresh_token
    on success. Returns None on failure (401, 5xx, timeout, missing rotation).
    """
    client_id = os.environ.get("JOBBER_CLIENT_ID", "")
    client_secret = os.environ.get("JOBBER_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        logger.warning("jobber: JOBBER_CLIENT_ID/SECRET missing; cannot refresh")
        await _persist_refresh_failure(cred["id"])
        return None

    try:
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
        # Log the exception TYPE only — never the message (may echo body).
        logger.warning("jobber: refresh exception type=%s", type(exc).__name__)
        await _persist_refresh_failure(cred["id"])
        return None

    if getattr(resp, "status_code", 500) != 200:
        logger.warning("jobber: refresh non-200 status=%d", resp.status_code)
        await _persist_refresh_failure(cred["id"])
        return None

    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        await _persist_refresh_failure(cred["id"])
        return None

    new_access = body.get("access_token")
    new_refresh = body.get("refresh_token")
    # Jobber mandates rotation — a missing new refresh_token is a contract
    # violation. Do NOT persist the old token (would break next cycle).
    if not new_access or not new_refresh:
        await _persist_refresh_failure(cred["id"])
        return None

    expiry_ms = _decode_jwt_exp_ms(new_access)
    expiry_iso = (
        datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc).isoformat()
        if expiry_ms
        else None
    )

    await _persist_refreshed_tokens(cred["id"], new_access, new_refresh, expiry_iso)

    return {
        **cred,
        "access_token": new_access,
        "refresh_token": new_refresh,
        "expiry_date": expiry_iso,
        "error_state": None,
    }


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
            # Proactive refresh if expired
            expiry_epoch = _expiry_to_epoch(cred.get("expiry_date"))
            if expiry_epoch and expiry_epoch <= datetime.now(timezone.utc).timestamp():
                refreshed = await _refresh_token(client, cred)
                if not refreshed:
                    return None
                cred = refreshed

            resp = await _post_graphql(client, cred["access_token"], phone_e164)

            # Reactive refresh + single retry on 401
            if resp is not None and getattr(resp, "status_code", 500) == 401:
                refreshed = await _refresh_token(client, cred)
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
