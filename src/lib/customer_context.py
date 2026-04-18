"""Phase 56 Plan 06 — Jobber + Xero customer context merge + concurrent fetch.

Exports:
  - `merge_customer_context(jobber, xero)`: field-level merge per CONTEXT D-07
    with `_sources` provenance markers per D-08. Returns None when both miss.
  - `fetch_merged_customer_context_bounded(tenant_id, phone_e164, timeout=0.8)`:
    races Jobber + Xero fetchers concurrently within the given per-provider
    budget. Timeout or exception for either provider silent-skips that half
    (sentry_sdk.capture_* with {tenant_id, provider, phone_hash} tags — never
    raw phone PII). Returns merged dict or None per D-11.

Merge rule (CONTEXT D-07):
  Jobber wins: client, recentJobs, lastVisitDate
  Xero wins:   outstandingBalance, lastPaymentDate, lastInvoices

Xero's `fetch_xero_customer_by_phone` returns SNAKE_CASE keys (`contact`,
`outstanding_balance`, `last_invoices`, `last_payment_date`). The merge
helper normalizes both providers into a single camelCase output shape
consumed by `prompt.py` + `check_customer_account`.

On no-match for BOTH providers, returns None and the downstream prompt
block is omitted entirely (CONTEXT D-11).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Optional

import sentry_sdk

from src.integrations.jobber import fetch_jobber_customer_by_phone
from src.integrations.xero import fetch_xero_customer_by_phone

logger = logging.getLogger(__name__)


# ---- Pure merge helper ----------------------------------------------------


def merge_customer_context(
    jobber: Optional[dict], xero: Optional[dict]
) -> Optional[dict]:
    """Field-level merge per CONTEXT D-07 with provenance markers per D-08.

    Args:
        jobber: result of fetch_jobber_customer_by_phone (camelCase) or None.
        xero:   result of fetch_xero_customer_by_phone   (snake_case) or None.

    Returns:
        Merged dict with keys {client, recentJobs, lastVisitDate,
        outstandingBalance, lastPaymentDate, lastInvoices, _sources}
        — only keys whose underlying data was available are populated.
        Returns None when BOTH providers miss (CONTEXT D-11).
    """
    if not jobber and not xero:
        return None

    merged: dict = {}
    sources: dict = {}

    # --- client (Jobber wins; Xero.contact is fallback with key rename) ---
    if jobber and jobber.get("client"):
        merged["client"] = jobber["client"]
        sources["client"] = "Jobber"
    elif xero and xero.get("contact"):
        contact = xero["contact"]
        # Normalize Xero's snake_case contact shape into a client-shaped dict.
        name_parts = [contact.get("first_name"), contact.get("last_name")]
        derived_name = " ".join(p for p in name_parts if p) or None
        merged["client"] = {
            "id": contact.get("contact_id") or contact.get("contactID") or contact.get("id"),
            "name": contact.get("name") or derived_name,
            "email": contact.get("emailAddress") or contact.get("email"),
        }
        sources["client"] = "Xero"

    # --- recentJobs (Jobber-only; Xero has no jobs concept) ---
    if jobber and jobber.get("recentJobs"):
        merged["recentJobs"] = jobber["recentJobs"]
        sources["recentJobs"] = "Jobber"

    # --- lastVisitDate (Jobber-only) ---
    if jobber and jobber.get("lastVisitDate"):
        merged["lastVisitDate"] = jobber["lastVisitDate"]
        sources["lastVisitDate"] = "Jobber"

    # --- outstandingBalance (Xero wins; Jobber is fallback) ---
    x_bal = xero.get("outstanding_balance") if xero else None
    j_bal = jobber.get("outstandingBalance") if jobber else None
    if x_bal is not None:
        merged["outstandingBalance"] = x_bal
        sources["outstandingBalance"] = "Xero"
    elif j_bal is not None:
        merged["outstandingBalance"] = j_bal
        sources["outstandingBalance"] = "Jobber"

    # --- lastPaymentDate (Xero-only — Jobber has no lastPaymentDate field) ---
    if xero and xero.get("last_payment_date"):
        merged["lastPaymentDate"] = xero["last_payment_date"]
        sources["lastPaymentDate"] = "Xero"

    # --- lastInvoices (Xero wins; Jobber.outstandingInvoices is fallback) ---
    if xero and xero.get("last_invoices"):
        merged["lastInvoices"] = xero["last_invoices"]
        sources["lastInvoices"] = "Xero"
    elif jobber and jobber.get("outstandingInvoices"):
        merged["lastInvoices"] = jobber["outstandingInvoices"]
        sources["lastInvoices"] = "Jobber"

    if not merged:
        return None
    merged["_sources"] = sources
    return merged


# ---- Bounded concurrent fetch --------------------------------------------


def _phone_hash(phone_e164: str) -> str:
    return hashlib.sha256((phone_e164 or "").encode()).hexdigest()[:8]


async def _fetch_with_bounds(
    provider_name: str,
    coro_factory,
    tenant_id: str,
    phone_e164: str,
    timeout_seconds: float,
) -> Optional[dict]:
    """Race a provider fetch against the timeout; silent-skip on failure.

    Returns the provider result OR None if timed out / raised. Emits Sentry
    with {tenant_id, provider, phone_hash} tags (no raw PII). Sentry
    failures are swallowed to prevent telemetry outage from breaking calls.
    """
    phash = _phone_hash(phone_e164)
    try:
        return await asyncio.wait_for(coro_factory(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        try:
            sentry_sdk.capture_message(
                f"{provider_name}_context_timeout",
                tags={
                    "tenant_id": tenant_id,
                    "provider": provider_name,
                    "phone_hash": phash,
                    "phase": "56",
                    "component": f"{provider_name}_context_fetch",
                },
            )
        except Exception:
            pass
        logger.info(
            "%s_context: timeout (tenant=%s phone_hash=%s)",
            provider_name, tenant_id, phash,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        try:
            sentry_sdk.capture_exception(
                exc,
                tags={
                    "tenant_id": tenant_id,
                    "provider": provider_name,
                    "phone_hash": phash,
                    "phase": "56",
                    "component": f"{provider_name}_context_fetch",
                },
            )
        except Exception:
            pass
        logger.info(
            "%s_context: exception (tenant=%s phone_hash=%s): %s",
            provider_name, tenant_id, phash, type(exc).__name__,
        )
        return None


async def fetch_merged_customer_context_bounded(
    tenant_id: str,
    phone_e164: str,
    timeout_seconds: float = 0.8,
) -> Optional[dict]:
    """Fetch Jobber + Xero CONCURRENTLY within per-provider budget; merge.

    Both providers race in parallel — adding Jobber does NOT extend the
    total budget beyond what Xero alone took in P55 (CONTEXT D-06). Any
    provider that times out or raises is silent-skipped; the other half
    still populates the merged dict. When BOTH miss, returns None.

    Never raises — a crash here would break the entire call path.
    """
    if not tenant_id or not phone_e164:
        return None

    # Create BOTH tasks before awaiting either (concurrent, not serial).
    jobber_task = asyncio.create_task(
        _fetch_with_bounds(
            "jobber",
            lambda: fetch_jobber_customer_by_phone(tenant_id, phone_e164),
            tenant_id, phone_e164, timeout_seconds,
        )
    )
    xero_task = asyncio.create_task(
        _fetch_with_bounds(
            "xero",
            lambda: fetch_xero_customer_by_phone(tenant_id, phone_e164),
            tenant_id, phone_e164, timeout_seconds,
        )
    )

    try:
        jobber_result, xero_result = await asyncio.gather(
            jobber_task, xero_task, return_exceptions=False
        )
    except Exception as exc:  # noqa: BLE001 — defense in depth
        try:
            sentry_sdk.capture_exception(
                exc,
                tags={
                    "tenant_id": tenant_id,
                    "phone_hash": _phone_hash(phone_e164),
                    "phase": "56",
                    "component": "merged_context_fetch",
                },
            )
        except Exception:
            pass
        return None

    return merge_customer_context(jobber=jobber_result, xero=xero_result)
