"""Google Maps Address Validation client for the LiveKit agent (Phase 61).

Voco's external client for Google's Address Validation API. Used by
src/tools/book_appointment.py and src/tools/capture_lead.py (Plan 03) to
validate caller-spoken service addresses, normalize them into Voco's
structured shape, and emit per-validate telemetry to gmaps_validate_events.

Public API
----------
- `validate_address_bounded(...)` — outer wrapper, never raises, always
  returns a Voco-shaped dict. The function tools call.
- `validate_address(...)` — bare HTTP call (no timeout wrapper, no Sentry
  capture, no telemetry insert). Exported for direct callers / tests.
- `map_verdict(google_response)` — pure mapper for Google's possibleNextAction
  enum → Voco's 6-state verdict.
- `map_components(addr)` — pure mapper for Google's addressComponents[] →
  Voco's D-D1 named-key dict.
- `VERDICT_ACCEPT`, `VERDICT_CONFIRM`, `VERDICT_CONFIRM_ADD_SUBPREMISES`,
  `VERDICT_FIX` — string constants matching Google's enum.

Voco return shape (all paths)
-----------------------------
```
{
    "verdict": "confirmed" | "confirmed_with_changes" | "unconfirmed"
               | "unsupported_region" | "error" | "skipped",
    "formatted_address": str | None,
    "place_id": str | None,
    "latitude": float | None,
    "longitude": float | None,
    "address_components": dict | None,   # D-D1 named-key shape (see map_components)
    "latency_ms": int,                    # wall-time
    "raw_status": int | None,             # HTTP status code (None on skipped/timeout)
}
```

Design choices
--------------
- Per-call `httpx.AsyncClient` (not module-level singleton) — matches the
  Voco LiveKit job lifecycle and the established xero.py / jobber.py shape.
- 1.5s hard timeout (D-C1). On timeout → verdict='error', booking proceeds
  unblocked. Booking is the user's actual goal; we never sacrifice the
  call path for validation polish.
- No retry loop inside the timeout budget (D-C1, RESEARCH §Don't Hand-Roll).
- No token-bucket rate limiter (D-C2 — Phase 61 is observability-only;
  Google's 6000 QPM cap is far above expected per-call load).
- Sentry capture ONLY when verdict='error' (D-A3, D-C3). Unsupported regions
  and missing API keys are observed via gmaps_validate_events aggregations
  — they should not page on-call.
- Country ISO code is read from `address.postalAddress.regionCode`, NOT
  from addressComponents (Pitfall 4).
- Raw Google response is never persisted (D-D1).

Caller is responsible for passing tenant_id + call_id from authenticated
context (the call DB lookup); never accept these from a tool argument or
untrusted source.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import httpx
import sentry_sdk

logger = logging.getLogger(__name__)

# ── Module constants ────────────────────────────────────────────────────────

GMAPS_VALIDATE_URL = "https://addressvalidation.googleapis.com/v1:validateAddress"

# D-C1: hard ceiling on per-call latency. Booking proceeds unblocked on timeout.
HTTP_TIMEOUT_SECONDS = 1.5

# D-A2: regions Voco actively supports. Observability via gmaps_validate_events
# aggregation by region_code, NOT enforcement — Google's API rejects unsupported
# regions with HTTP 400 + INVALID_ARGUMENT, which we map to verdict='unsupported_region'.
SUPPORTED_REGION_CODES = frozenset({"US", "CA", "SG"})

# CONTEXT D-G3 cost estimate ($0.017 per validate × 100 = 1700 micro-cents).
# Recorded on each successful (or unsupported_region) telemetry row so the
# billing-overage projection can roll up gmaps_validate_events without
# round-tripping to Google's billing API.
COST_MICRO_CENTS_PER_VALIDATE = 1700

# Verdict enum from Google's build-validation-logic page.
VERDICT_ACCEPT = "ACCEPT"
VERDICT_CONFIRM = "CONFIRM"
VERDICT_CONFIRM_ADD_SUBPREMISES = "CONFIRM_ADD_SUBPREMISES"
VERDICT_FIX = "FIX"

# Voco's 6-state verdict — derived from Google's possibleNextAction.
# CONFIRM_ADD_SUBPREMISES collapses to confirmed_with_changes per D-B1
# (Voco doesn't probe for unit numbers; the agent reads back what was found
# and invites caller confirmation per the D-B3 confirmed_with_changes path).
_VOCO_VERDICT_MAP = {
    VERDICT_ACCEPT: "confirmed",
    VERDICT_CONFIRM: "confirmed_with_changes",
    VERDICT_CONFIRM_ADD_SUBPREMISES: "confirmed_with_changes",
    VERDICT_FIX: "unconfirmed",
}


# ── Pure mappers ────────────────────────────────────────────────────────────


def map_verdict(google_response: dict) -> str:
    """Map Google's `verdict.possibleNextAction` enum to Voco's verdict.

    ACCEPT                     → confirmed
    CONFIRM                    → confirmed_with_changes
    CONFIRM_ADD_SUBPREMISES    → confirmed_with_changes  (D-B1: don't probe units)
    FIX                        → unconfirmed
    <missing or unknown>       → unconfirmed             (defensive)

    Pure function, no I/O. Returns one of the 3 in-band Voco verdicts;
    out-of-band states (unsupported_region, error, skipped) are set by the
    HTTP-level code paths in validate_address.
    """
    if not isinstance(google_response, dict):
        return "unconfirmed"
    verdict_block = (google_response.get("result") or {}).get("verdict") or {}
    action = verdict_block.get("possibleNextAction")
    if action is None:
        return "unconfirmed"
    return _VOCO_VERDICT_MAP.get(action, "unconfirmed")


def map_components(addr: dict) -> dict:
    """Map Google's `address` block to Voco's D-D1 named-key dict.

    Args:
        addr: `result.address` from a successful Address Validation response,
              or `{}` for defensive empty-input handling.

    Returns:
        Dict with 9 fixed keys per D-D1:
          street_number, route, subpremise,
          locality, admin_area_level_1, admin_area_level_2,
          postal_code, country, country_code

    CRITICAL — `country_code` is read from `addr.postalAddress.regionCode`,
    NOT from addressComponents (Pitfall 4). Address Validation's
    addressComponents only carries the long-form country name; the ISO
    short code lives in postalAddress.regionCode.

    Locality fallback: `locality` first, then `sublocality`, then
    `sublocality_level_1` — handles SG addresses where locality may be
    absent (Pitfall A3 / SG HDB).

    Pure function, no I/O. Defensive against missing keys / empty dict input.
    """
    if not isinstance(addr, dict):
        addr = {}

    # Build a flat {componentType: componentName.text} dict from
    # addressComponents[]. Pitfall 3: componentType is at the top level of
    # each component, NOT nested under componentName.componentType.
    components: dict = {}
    for c in addr.get("addressComponents") or []:
        ctype = c.get("componentType")
        cname = (c.get("componentName") or {}).get("text")
        if ctype and cname:
            components[ctype] = cname

    # Pitfall 4: country_code from postalAddress.regionCode, NOT from
    # addressComponents (which only has long-form country name).
    postal_address = addr.get("postalAddress") or {}
    country_code = postal_address.get("regionCode")

    return {
        "street_number": components.get("street_number"),
        "route": components.get("route"),
        "subpremise": components.get("subpremise"),
        # SG addresses lack `locality`; fall back to sublocality / sublocality_level_1.
        "locality": (
            components.get("locality")
            or components.get("sublocality")
            or components.get("sublocality_level_1")
        ),
        "admin_area_level_1": components.get("administrative_area_level_1"),
        "admin_area_level_2": components.get("administrative_area_level_2"),
        "postal_code": components.get("postal_code"),
        # Long-form country name (e.g. "United States", "Singapore").
        "country": components.get("country"),
        # ISO short code (e.g. "US", "SG") — from postalAddress.regionCode.
        "country_code": country_code,
    }


# ── Internal helpers ────────────────────────────────────────────────────────


def _empty_components() -> dict:
    """The D-D1 named-key dict with every value set to None (used on
    error / skipped / timeout paths so the return shape stays stable)."""
    return {
        "street_number": None,
        "route": None,
        "subpremise": None,
        "locality": None,
        "admin_area_level_1": None,
        "admin_area_level_2": None,
        "postal_code": None,
        "country": None,
        "country_code": None,
    }


def _voco_result(
    *,
    verdict: str,
    formatted_address: Optional[str] = None,
    place_id: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    address_components: Optional[dict] = None,
    latency_ms: int = 0,
    raw_status: Optional[int] = None,
) -> dict:
    """Build a Voco-shaped return dict. Stable key set across all paths."""
    return {
        "verdict": verdict,
        "formatted_address": formatted_address,
        "place_id": place_id,
        "latitude": latitude,
        "longitude": longitude,
        "address_components": address_components or _empty_components(),
        "latency_ms": latency_ms,
        "raw_status": raw_status,
    }


def _is_unsupported_region_400(status_code: int, body_text: str) -> bool:
    """Classify HTTP 400 responses: unsupported region vs other client error.

    Google's body for unsupported regions contains 'INVALID_ARGUMENT',
    'regionCode', or 'Invalid region'. Other 400s (malformed request, etc.)
    fall through to verdict='error'.
    """
    if status_code != 400:
        return False
    if not body_text:
        return False
    haystack = body_text.lower()
    return any(
        marker in haystack
        for marker in ("invalid_argument", "regioncode", "invalid region")
    )


# ── HTTP layer ──────────────────────────────────────────────────────────────


async def validate_address(
    *,
    region_code: str,
    address_lines: list,
    postal_code: Optional[str] = None,
    locality: Optional[str] = None,
) -> dict:
    """Bare HTTP call to Google's Address Validation API.

    Returns a Voco-shaped dict. Does NOT wrap with asyncio.timeout, does NOT
    capture Sentry, does NOT write telemetry — those are validate_address_bounded's
    responsibilities. Exported separately for direct/test use.

    Reads GOOGLE_MAPS_API_KEY from env. If missing → verdict='skipped'
    immediately (D-G1 graceful degradation; the call path proceeds unblocked).
    """
    t0 = time.monotonic()

    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        logger.info("[phase61] GOOGLE_MAPS_API_KEY missing — verdict=skipped")
        return _voco_result(
            verdict="skipped",
            latency_ms=0,
            raw_status=None,
        )

    # WR-02 (Phase 61.1): short-circuit empty / whitespace-only address_lines
    # BEFORE the HTTP call. Without this guard, Google returns 400
    # INVALID_ARGUMENT and `_is_unsupported_region_400` matches the
    # `invalid_argument` substring → misclassified as `unsupported_region`
    # (false billing rollup + lost Sentry signal). The `error` verdict is the
    # correct posture for "we never captured an address" — the bounded
    # wrapper's Sentry gate (D-A3) will then fire on this verdict.
    if not address_lines or not any(
        (line or "").strip() for line in address_lines
    ):
        latency_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "[phase61] empty address_lines — short-circuit verdict=error region_code=%s",
            region_code,
        )
        return _voco_result(
            verdict="error",
            latency_ms=latency_ms,
            raw_status=None,
        )

    # Build request body per RESEARCH §Code Examples 1.
    address_block: dict = {
        "regionCode": region_code,
        "addressLines": list(address_lines or []),
    }
    if postal_code:
        address_block["postalCode"] = postal_code
    if locality:
        address_block["locality"] = locality

    body = {"address": address_block}

    # Per-call AsyncClient. The 1.5s timeout here matches D-C1; the outer
    # validate_address_bounded wrapper also enforces it via asyncio.timeout
    # (belt-and-suspenders — socket-level termination AND task-level cancel).
    timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS)
    raw_status: Optional[int] = None

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{GMAPS_VALIDATE_URL}?key={api_key}",
                json=body,
                headers={"Content-Type": "application/json"},
            )
            raw_status = getattr(resp, "status_code", None)

            # Read the response body text for error classification BEFORE
            # parsing JSON — some 4xx responses may not be valid JSON.
            try:
                body_text = resp.text or ""
            except Exception:  # noqa: BLE001
                body_text = ""

            if raw_status == 400 and _is_unsupported_region_400(raw_status, body_text):
                # D-A3: unsupported_region is a normal observability state, not an error.
                logger.info(
                    "[phase61] unsupported_region region_code=%s status=%d",
                    region_code,
                    raw_status,
                )
                latency_ms = int((time.monotonic() - t0) * 1000)
                return _voco_result(
                    verdict="unsupported_region",
                    latency_ms=latency_ms,
                    raw_status=raw_status,
                )

            if raw_status != 200:
                # 401, 403, 404, 429, other 4xx, 5xx → verdict='error'.
                logger.warning(
                    "[phase61] validate non-200 status=%s region_code=%s",
                    raw_status,
                    region_code,
                )
                latency_ms = int((time.monotonic() - t0) * 1000)
                return _voco_result(
                    verdict="error",
                    latency_ms=latency_ms,
                    raw_status=raw_status,
                )

            # Successful 200 — parse and map.
            try:
                google_response = resp.json()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[phase61] response JSON parse failed: %s", exc)
                latency_ms = int((time.monotonic() - t0) * 1000)
                return _voco_result(
                    verdict="error",
                    latency_ms=latency_ms,
                    raw_status=raw_status,
                )

    except asyncio.TimeoutError:
        # Inner-client timeout (rare — outer wrapper usually fires first).
        latency_ms = int((time.monotonic() - t0) * 1000)
        return _voco_result(
            verdict="error",
            latency_ms=latency_ms,
            raw_status=None,
        )
    except Exception as exc:  # noqa: BLE001
        # Any other network/transport error → verdict='error'. Outer
        # validate_address_bounded handles Sentry capture.
        logger.warning(
            "[phase61] validate exception type=%s region_code=%s",
            type(exc).__name__,
            region_code,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        return _voco_result(
            verdict="error",
            latency_ms=latency_ms,
            raw_status=raw_status,
        )

    # Map success response into Voco shape.
    voco_verdict = map_verdict(google_response)
    result_block = google_response.get("result") or {}
    addr_block = result_block.get("address") or {}
    geocode_block = result_block.get("geocode") or {}
    location_block = geocode_block.get("location") or {}

    formatted_address = addr_block.get("formattedAddress")
    place_id = geocode_block.get("placeId")
    latitude = location_block.get("latitude")
    longitude = location_block.get("longitude")
    address_components = map_components(addr_block)

    latency_ms = int((time.monotonic() - t0) * 1000)
    return _voco_result(
        verdict=voco_verdict,
        formatted_address=formatted_address,
        place_id=place_id,
        latitude=float(latitude) if latitude is not None else None,
        longitude=float(longitude) if longitude is not None else None,
        address_components=address_components,
        latency_ms=latency_ms,
        raw_status=raw_status,
    )


# ── Outer wrapper (never raises, telemetry + Sentry gate) ──────────────────


async def validate_address_bounded(
    tenant_id: Optional[str],
    call_id: Optional[str],
    *,
    region_code: str,
    address_lines: list,
    postal_code: Optional[str] = None,
    locality: Optional[str] = None,
    supabase=None,
    timeout_seconds: float = HTTP_TIMEOUT_SECONDS,
) -> dict:
    """Outer wrapper. Never raises. Always returns a Voco-shaped dict.

    Responsibilities beyond validate_address():
      1. Hard `asyncio.timeout(timeout_seconds)` (D-C1) — socket-level cap
         is already in validate_address; this is the task-level cap.
      2. Sentry capture ONLY when verdict='error' (D-A3, D-C3).
         Unsupported regions and skipped paths NEVER page Sentry.
      3. Per-validate telemetry insert into gmaps_validate_events (D-C2')
         when `supabase` kwarg is provided. Telemetry failure NEVER raises.

    Args:
        tenant_id: Voco tenant_id (UUID), resolved server-side. May be None
                   for early-call paths (telemetry tags as 'unknown').
        call_id: Voco call_id (UUID), resolved server-side. May be None.
        region_code: ISO 3166-1 alpha-2 country code (e.g. "US", "SG").
        address_lines: List of address line strings (e.g. ["1600 Amphitheatre Pkwy"]).
        postal_code: Optional postal/zip code.
        locality: Optional city/locality name.
        supabase: Optional service-role supabase client (sync supabase-py).
                  When provided, one row is inserted into gmaps_validate_events.
                  When None (e.g. tests), telemetry is skipped silently.
        timeout_seconds: Hard task-level timeout. Defaults to 1.5s (D-C1).

    Returns:
        Voco-shaped dict (see module docstring).
    """
    t0 = time.monotonic()
    captured_exc: Optional[BaseException] = None

    try:
        # asyncio.timeout was added in 3.11; we already require Python 3.11+
        # via pyproject.toml. asyncio.wait_for is the broadly-equivalent
        # fallback used elsewhere in the codebase (xero.py).
        result = await asyncio.wait_for(
            validate_address(
                region_code=region_code,
                address_lines=address_lines,
                postal_code=postal_code,
                locality=locality,
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        captured_exc = exc
        latency_ms = int(timeout_seconds * 1000)
        result = _voco_result(
            verdict="error",
            latency_ms=latency_ms,
            raw_status=None,
        )
    except Exception as exc:  # noqa: BLE001 — outer wrapper must not propagate
        captured_exc = exc
        latency_ms = int((time.monotonic() - t0) * 1000)
        result = _voco_result(
            verdict="error",
            latency_ms=latency_ms,
            raw_status=None,
        )

    # ── Sentry gate (D-A3, D-C3) ────────────────────────────────────────────
    # Capture ONLY on verdict='error'. Unsupported regions and skipped paths
    # are observed via gmaps_validate_events aggregations — they should never
    # page on-call. Paging on those would create alert fatigue and obscure
    # real failures.
    if result.get("verdict") == "error":
        try:
            exc_to_capture = captured_exc or RuntimeError(
                "gmaps validate returned verdict=error"
            )
            sentry_sdk.capture_exception(
                exc_to_capture,
                tags={
                    "tenant_id": tenant_id or "unknown",
                    "call_id": call_id or "unknown",
                    "phase": "61",
                    "component": "google_maps_validate",
                    "region_code": region_code or "unknown",
                },
            )
        except Exception:  # noqa: BLE001
            # Telemetry must never crash the caller. Sentry failures are
            # silently absorbed.
            pass

    # ── Telemetry insert (D-C2') ────────────────────────────────────────────
    # One row per validate attempt to gmaps_validate_events. Schema:
    #   tenant_id, call_id, verdict, latency_ms, region_code, cost_micro_cents
    # cost_micro_cents is 0 for skipped/error (no Google billing); the
    # successful + unsupported_region paths get COST_MICRO_CENTS_PER_VALIDATE.
    if supabase is not None:
        if not tenant_id:
            # WR-01 (Phase 61.1): gmaps_validate_events.tenant_id is NOT NULL.
            # Skipping the insert with an explicit warn log preserves D-C2'
            # observability semantics ("we know why this row is missing")
            # instead of swallowing a constraint violation in a bare except.
            logger.warning(
                "[phase61] gmaps_validate_events insert skipped: tenant_id is None (call_id=%s)",
                call_id or "unknown",
            )
        else:
            try:
                verdict = result.get("verdict")
                cost = (
                    COST_MICRO_CENTS_PER_VALIDATE
                    if verdict not in ("skipped", "error")
                    else 0
                )
                payload = {
                    "tenant_id": tenant_id,
                    "call_id": call_id,
                    "verdict": verdict,
                    "latency_ms": result.get("latency_ms"),
                    "cost_micro_cents": cost,
                    "region_code": region_code,
                }

                # supabase-py is sync — wrap the chain in to_thread so we don't
                # block the event loop. Test harnesses pass a MagicMock that
                # accepts the chain synchronously; to_thread handles both.
                def _insert() -> None:
                    supabase.table("gmaps_validate_events").insert(payload).execute()

                await asyncio.to_thread(_insert)
            except Exception as exc:  # noqa: BLE001
                # Telemetry failures must never block the call path. Log at
                # warning level (not error — gmaps_validate_events is observability,
                # not a user-facing failure).
                logger.warning(
                    "[phase61] gmaps_validate_events insert failed: %s",
                    exc,
                )

    return result
