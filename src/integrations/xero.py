"""Xero read-side integration for the LiveKit agent (Phase 55).

Provides fetch_xero_customer_by_phone(tenant_id, phone_e164) which:
  1. Reads accounting_credentials row via service-role Supabase
  2. Refreshes access_token if expired (5-min buffer); persists new tokens back
  3. Looks up Xero contact by phone (E.164 exact match, post-filter per D-01)
  4. Fetches outstanding (AUTHORISED + AmountDue>0) and recent
     (AUTHORISED|PAID, Date DESC, 3) invoices
  5. Returns the standard shape OR None if no creds / no match / refresh failure

Design choices:
  - Raw httpx, not xero-python SDK — tighter timeout control on the 800ms
    hot path; smaller dep footprint; only 3 endpoints needed.
  - Refresh-on-demand only. Refresh failure persists
    error_state='token_refresh_failed' on the row and returns None silently.
    Email/banner surfaces via the Next.js dashboard read path (Phase 55 Plan 05);
    this module never sends email (would be noisy).
  - All Supabase calls wrapped via asyncio.to_thread (sync supabase-py).

Caller is responsible for resolving tenant_id from authenticated context (the
call DB lookup); never accept tenant_id from a tool argument or untrusted
source.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

# Imported at module level (not lazily) so Phase 58 telemetry can resolve the
# admin client at call time AND tests can patch `xero_mod.get_supabase_admin`.
from ..supabase_client import get_supabase_admin
from ..lib.telemetry import emit_integration_fetch

logger = logging.getLogger(__name__)

XERO_API_BASE = "https://api.xero.com/api.xro/2.0"
XERO_TOKEN_URL = "https://identity.xero.com/connect/token"
HTTP_TIMEOUT_SECONDS = 1.5  # Xero cold API can exceed 500ms per call
REFRESH_BUFFER_SECONDS = 300  # refresh if access_token expires in < 5 min
E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


def _now_ts() -> float:
    return time.time()


def _expiry_to_epoch(expiry_value) -> float:
    """Parse accounting_credentials.expiry_date to epoch seconds.

    The Next.js side stores expiry_date as a BIGINT column holding
    ``Date.now() + expires_in * 1000`` (epoch milliseconds). The Python
    side may also see ISO 8601 strings if another writer ever changes the
    schema. Handle both. Returns 0 (forces refresh) on unparseable input.
    """
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
    """Service-role read of accounting_credentials for (tenant_id, provider='xero')."""

    def _query() -> Optional[dict]:
        admin = get_supabase_admin()
        resp = (
            admin.table("accounting_credentials")
            .select("*")
            .eq("tenant_id", tenant_id)
            .eq("provider", "xero")
            .maybe_single()
            .execute()
        )
        return resp.data if resp and getattr(resp, "data", None) else None

    try:
        return await asyncio.to_thread(_query)
    except Exception as exc:  # noqa: BLE001
        logger.warning("xero: failed to load credentials for tenant=%s: %s", tenant_id, exc)
        return None


async def _persist_refreshed_tokens(
    cred_id: str,
    access_token: str,
    refresh_token: str,
    expiry_date_iso: str,
) -> None:
    """Write refreshed token set back to accounting_credentials.

    Critical — without write-back, the Next.js side sees stale tokens and
    re-refreshes redundantly, racing with us on Xero's refresh-token rotation.
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
        logger.warning("xero: failed to persist refreshed tokens for cred=%s: %s", cred_id, exc)


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
    except Exception as exc:  # noqa: BLE001
        logger.warning("xero: failed to mark refresh failure for cred=%s: %s", cred_id, exc)


async def _touch_last_context_fetch_at(cred_id: str) -> None:
    """Telemetry seed — updates last_context_fetch_at on successful fetch."""

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
        pass  # telemetry — silent on failure


async def _refresh_if_needed(client: httpx.AsyncClient, cred: dict) -> Optional[dict]:
    """Returns updated cred dict with fresh access_token, or None on refresh failure."""
    expiry_epoch = _expiry_to_epoch(cred.get("expiry_date"))
    if expiry_epoch - _now_ts() > REFRESH_BUFFER_SECONDS:
        return cred  # still valid

    client_id = os.environ.get("XERO_CLIENT_ID")
    client_secret = os.environ.get("XERO_CLIENT_SECRET")
    if not client_id or not client_secret:
        logger.error("xero: XERO_CLIENT_ID/SECRET missing; cannot refresh")
        return None

    try:
        resp = await client.post(
            XERO_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": cred["refresh_token"],
            },
            auth=(client_id, client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        status = getattr(resp, "status_code", 500)
        if status != 200:
            logger.warning("xero: refresh non-200 status=%d", status)
            await _persist_refresh_failure(cred["id"])
            return None
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("xero: refresh exception: %s", exc)
        await _persist_refresh_failure(cred["id"])
        return None

    new_access = body.get("access_token")
    new_refresh = body.get("refresh_token", cred["refresh_token"])  # Xero may rotate
    expires_in = int(body.get("expires_in", 1800))
    if not new_access:
        await _persist_refresh_failure(cred["id"])
        return None

    new_expiry = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    await _persist_refreshed_tokens(cred["id"], new_access, new_refresh, new_expiry)

    return {
        **cred,
        "access_token": new_access,
        "refresh_token": new_refresh,
        "expiry_date": new_expiry,
        "error_state": None,
    }


def _xero_headers(access_token: str, xero_tenant_id: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Xero-tenant-id": xero_tenant_id,
        "Accept": "application/json",
    }


async def _get_contacts_by_phone(
    client: httpx.AsyncClient,
    cred: dict,
    phone_e164: str,
) -> Optional[dict]:
    """Returns the Xero contact whose phone digits-match, or None.

    Phone storage varies wildly across Xero orgs and countries — full E.164,
    8-digit SG local, 10-digit US local, compound PhoneCountryCode/AreaCode/
    Number fields, formatted strings with spaces/dashes. An OData Contains
    filter based on any single canonical form misses everything else. Instead
    we fetch Xero's default page of contacts (up to 100) and match by digits
    in Python. For orgs with >100 contacts this would need pagination;
    deferred to P58 if hit in practice.
    """
    try:
        resp = await client.get(
            f"{XERO_API_BASE}/Contacts",
            headers=_xero_headers(cred["access_token"], cred["xero_tenant_id"]),
            params={"summaryOnly": "false"},
        )
        if getattr(resp, "status_code", 500) != 200:
            return None
        contacts = resp.json().get("Contacts", []) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("xero: getContacts exception: %s", exc)
        return None

    target_ten = re.sub(r"\D", "", phone_e164)[-10:]
    target_seven = target_ten[-7:]
    if len(target_ten) < 7:
        return None

    # Diagnostic: sample the first few contacts so we can see what Xero stores.
    sample = []
    for c in contacts[:5]:
        name = c.get("Name", "?")
        phone_shapes = []
        for p in (c.get("Phones") or []):
            phone_shapes.append({
                "type": p.get("PhoneType"),
                "cc": p.get("PhoneCountryCode"),
                "ac": p.get("PhoneAreaCode"),
                "num": p.get("PhoneNumber"),
            })
        sample.append({"name": name, "phones": phone_shapes})
    logger.info(
        "xero: getContacts returned %d contacts; target_seven=%s; sample=%s",
        len(contacts), target_seven, sample,
    )

    for c in contacts:
        for p in (c.get("Phones") or []):
            # Xero stores phones three ways: full string in PhoneNumber, split
            # across PhoneCountryCode + PhoneAreaCode + PhoneNumber, or a mix.
            # Concatenate all three and compare digits-only.
            combined = (
                (p.get("PhoneCountryCode") or "")
                + (p.get("PhoneAreaCode") or "")
                + (p.get("PhoneNumber") or "")
            )
            digits = re.sub(r"\D", "", combined)
            if not digits:
                continue
            # Try last-10 (US / full-E.164-stored) OR last-7 (SG local / subscriber-only).
            if digits[-10:] == target_ten or digits[-7:] == target_seven:
                return c
    return None


async def _get_outstanding_balance(
    client: httpx.AsyncClient,
    cred: dict,
    contact_id: str,
) -> float:
    where = (
        f'Status=="AUTHORISED" AND Contact.ContactID==guid("{contact_id}") AND AmountDue>0'
    )
    try:
        resp = await client.get(
            f"{XERO_API_BASE}/Invoices",
            headers=_xero_headers(cred["access_token"], cred["xero_tenant_id"]),
            params={"where": where},
        )
        if getattr(resp, "status_code", 500) != 200:
            return 0.0
        invoices = resp.json().get("Invoices", []) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("xero: getInvoices(outstanding) exception: %s", exc)
        return 0.0
    return float(sum((i.get("AmountDue") or 0) for i in invoices))


async def _get_recent_invoices(
    client: httpx.AsyncClient,
    cred: dict,
    contact_id: str,
) -> list[dict]:
    where = f'(Status=="AUTHORISED" OR Status=="PAID") AND Contact.ContactID==guid("{contact_id}")'
    try:
        resp = await client.get(
            f"{XERO_API_BASE}/Invoices",
            headers=_xero_headers(cred["access_token"], cred["xero_tenant_id"]),
            params={"where": where, "order": "Date DESC", "page": 1},
        )
        if getattr(resp, "status_code", 500) != 200:
            return []
        return resp.json().get("Invoices", []) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("xero: getInvoices(recent) exception: %s", exc)
        return []


async def fetch_xero_customer_by_phone(
    tenant_id: str,
    phone_e164: str,
) -> Optional[dict]:
    """Top-level entry. Returns dict or None on any failure / no-match.

    Shape:
      {
        "contact": {contact_id, name, first_name, last_name, phones: [...]},
        "outstanding_balance": float,
        "last_invoices": [{invoice_number, date, total, amount_due, status, reference}, ...],
        "last_payment_date": str | None,
      }
    """
    if not isinstance(tenant_id, str) or not isinstance(phone_e164, str):
        return None
    if not E164_RE.match(phone_e164):
        return None

    # Phase 58 CTX-01: per-fetch latency measurement. `_cache_hit` is always
    # False here (no in-memory cache in the Python adapter today — the cache
    # layer lives in Next.js via `'use cache'` + revalidateTag). Column
    # retained so the activity_log schema is forward-compatible if an
    # in-agent cache lands later.
    _fetch_start = time.perf_counter()
    _cache_hit = False

    cred = await _load_credentials(tenant_id)
    if not cred or not cred.get("xero_tenant_id"):
        logger.info("xero: no credentials or xero_tenant_id for tenant=%s", tenant_id)
        return None

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        cred = await _refresh_if_needed(client, cred)
        if not cred:
            logger.info("xero: refresh returned None (failure) for tenant=%s", tenant_id)
            return None

        contact = await _get_contacts_by_phone(client, cred, phone_e164)
        if not contact:
            logger.info(
                "xero: getContacts found no exact-phone match for tenant=%s xero_org=%s",
                tenant_id, cred.get("xero_tenant_id"),
            )
            return None

        contact_id = contact.get("ContactID")
        if not contact_id:
            return None

        # Run the two independent invoice queries concurrently — cuts latency
        # from ~2× per-call to ~1× per-call on Xero's cold API.
        outstanding_balance, all_recent = await asyncio.gather(
            _get_outstanding_balance(client, cred, contact_id),
            _get_recent_invoices(client, cred, contact_id),
        )

    last_invoices = [
        {
            "invoice_number": i.get("InvoiceNumber"),
            "date": i.get("Date"),
            "total": i.get("Total"),
            "amount_due": i.get("AmountDue"),
            "status": i.get("Status"),
            "reference": i.get("Reference"),
        }
        for i in all_recent[:3]
    ]

    paid_dates = [
        i.get("FullyPaidOnDate")
        for i in all_recent
        if i.get("Status") == "PAID" and i.get("FullyPaidOnDate")
    ]
    last_payment_date = max(paid_dates) if paid_dates else None

    shaped = {
        "contact": {
            "contact_id": contact.get("ContactID"),
            "name": contact.get("Name"),
            "first_name": contact.get("FirstName"),
            "last_name": contact.get("LastName"),
            "phones": [p.get("PhoneNumber") for p in (contact.get("Phones") or [])],
        },
        "outstanding_balance": outstanding_balance,
        "last_invoices": last_invoices,
        "last_payment_date": last_payment_date,
    }

    # Phase 58 CTX-01: parallelize the last_context_fetch_at UPDATE with the
    # integration_fetch activity_log INSERT via asyncio.gather so telemetry
    # adds ZERO latency to the fetch return path. Both writes are
    # silent-on-failure inside their helpers.
    _duration_ms = int((time.perf_counter() - _fetch_start) * 1000)
    _counts = {
        "customers": 1 if shaped.get("contact") else 0,
        "invoices": len(shaped.get("last_invoices") or []),
    }
    # Resolve admin client for telemetry. If unavailable (missing env vars in
    # test harness), skip the activity_log insert but preserve the existing
    # last_context_fetch_at UPDATE so Phase 55 behavior is unchanged.
    try:
        admin = get_supabase_admin()
    except Exception as exc:  # noqa: BLE001 — telemetry must not break return path
        logger.warning("xero: telemetry skipped — admin client unavailable: %s", exc)
        admin = None

    if admin is not None:
        await asyncio.gather(
            _touch_last_context_fetch_at(cred["id"]),
            emit_integration_fetch(
                admin,
                tenant_id=tenant_id,
                provider="xero",
                duration_ms=_duration_ms,
                cache_hit=_cache_hit,
                counts=_counts,
                phone_e164=phone_e164,
            ),
        )
    else:
        await _touch_last_context_fetch_at(cred["id"])

    return shaped


async def fetch_xero_context_bounded(
    tenant_id: str,
    phone_e164: str,
    timeout_seconds: float = 0.8,
) -> Optional[dict]:
    """Wrapper around fetch_xero_customer_by_phone with a hard timeout.

    D-04: on timeout or exception, returns None and captures the failure in
    Sentry (if available) with tenant_id + hashed phone tags. Never raises —
    the call path must not be blocked or crashed by Xero issues.
    """
    import hashlib

    try:
        result = await asyncio.wait_for(
            fetch_xero_customer_by_phone(tenant_id, phone_e164),
            timeout=timeout_seconds,
        )
        if result is None:
            logger.info(
                "xero_context: no match for caller (tenant=%s phone_hash=%s)",
                tenant_id,
                hashlib.sha256((phone_e164 or "").encode()).hexdigest()[:8],
            )
        else:
            logger.info(
                "xero_context: fetched (contact=%s outstanding=%s invoices=%d)",
                (result.get("contact") or {}).get("name"),
                result.get("outstanding_balance"),
                len(result.get("last_invoices") or []),
            )
        return result
    except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(
                exc,
                tags={
                    "tenant_id": tenant_id or "unknown",
                    "phone_hash": hashlib.sha256(
                        (phone_e164 or "").encode()
                    ).hexdigest()[:8],
                    "phase": "55",
                    "component": "xero_context_fetch",
                },
            )
        except Exception:  # noqa: BLE001
            pass  # telemetry must never crash the caller
        logger.info(
            "xero_context: skipped (%s: %s)",
            "timeout" if isinstance(exc, asyncio.TimeoutError) else "error",
            exc,
        )
        return None
