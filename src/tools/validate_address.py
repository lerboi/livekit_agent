"""
validate_address tool -- early, mid-call address validation (2026-06-10).

Lets the agent validate the service address the MOMENT the caller finishes
saying it (after a one-sentence filler), instead of waiting for the
booking/lead commit like Phase 61 did. The Phase 61 plumbing is unchanged:
this tool goes through `validate_address_with_region_fallback`, which wraps
the same `validate_address_bounded` (1.5s hard timeout per attempt, never
raises, gmaps_validate_events telemetry per attempt, Sentry on verdict=error
only) and adds an automatic caller-region (caller-ID-derived) second attempt
when the tenant-region verdict is unhelpful.

The full bounded result is cached on `deps["_validated_address"]` so
book_appointment / capture_lead can reuse it (no second Google call) when
the address the model passes them matches what was validated here. On any
mismatch those tools fall back to validating themselves, exactly as before
— booking NEVER blocks on (or is gated by) this tool having run.

Registered ALWAYS-ON in src/tools/__init__.py: capture_lead needs addresses
too and is itself always registered.
"""

from __future__ import annotations

import logging
import time

from livekit.agents import function_tool, RunContext

from ..integrations.google_maps import validate_address_with_region_fallback

logger = logging.getLogger(__name__)


_SCHEMA = {
    "name": "validate_address",
    "description": (
        "Validate the service address the moment the caller finishes giving "
        "it — do not wait for booking. Pass the pieces exactly as the caller "
        "said them. The return tells you whether the address came back "
        "confirmed, corrected, or unclear, and exactly what to say next. "
        "Speak a one-sentence filler first ('Let me just check that "
        "address…'), then invoke in the same turn. This tool's return is a "
        "state+directive string — data for you, not to be read aloud."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "street": {
                "type": "string",
                "description": (
                    "Street portion of the address as the caller said it "
                    "(house/block number + street name)."
                ),
            },
            "unit": {
                "type": "string",
                "description": (
                    "Unit / apartment / suite number, if the caller gave one. "
                    "Empty string if none."
                ),
            },
            "postal_code": {
                "type": "string",
                "description": (
                    "Postal / zip code, if the caller gave one. Empty string "
                    "if not given yet."
                ),
            },
            "city": {
                "type": "string",
                "description": (
                    "City / locality, if the caller gave one. Empty string "
                    "if not given."
                ),
            },
        },
        "required": ["street"],
    },
}


def _norm(value: str | None) -> str:
    """Casefold + strip normalization for cache-key comparison."""
    return (value or "").strip().casefold()


def get_cached_validation(deps: dict, street: str, postal_code: str) -> dict | None:
    """Return the cached bounded-validation result from an earlier
    validate_address call IF the input address matches; else None.

    Match rule: normalized street AND normalized postal code must be equal.
    Unit differences are deliberately tolerated (callers often add/refine the
    unit between the early validation and the booking commit; the unit does
    not change the Google verdict for the building).

    A cached verdict of 'error' is never reused — that was a transient
    timeout/HTTP failure, and the booking-time fallback validation deserves a
    fresh attempt rather than inheriting the failure.
    """
    cached = deps.get("_validated_address")
    if not cached:
        return None
    result = cached.get("result") or {}
    if result.get("verdict") == "error":
        return None
    cached_input = cached.get("input") or {}
    if _norm(cached_input.get("street")) != _norm(street):
        return None
    if _norm(cached_input.get("postal_code")) != _norm(postal_code):
        return None
    return result


def _missing_component_hint(result: dict, postal_code: str) -> str:
    """Best-effort hint for the unconfirmed branch: which piece looked
    unclear to the validation service."""
    components = result.get("address_components") or {}
    if not (postal_code or components.get("postal_code")):
        return "postal_code"
    if not components.get("street_number"):
        return "street_number"
    return "street"


def create_validate_address_tool(deps: dict):
    @function_tool(raw_schema=_SCHEMA)
    async def validate_address(raw_arguments: dict, context: RunContext) -> str:
        street = (raw_arguments.get("street") or "").strip()
        unit = (raw_arguments.get("unit") or "").strip()
        postal_code = (raw_arguments.get("postal_code") or "").strip()
        city = (raw_arguments.get("city") or "").strip()

        # The caller-spoken form, used verbatim for the not-validated paths.
        as_given = ", ".join(p for p in [street, unit, city, postal_code] if p)

        region_code = (deps.get("country") or "US").upper()
        address_lines = (
            [", ".join(p for p in [street, unit] if p)]
            if (street or unit)
            else []
        )

        # The fallback orchestrator is contractually never-raising, but this
        # tool must ALSO never raise (an exception here would surface as a
        # failed tool call mid-conversation) — belt and braces.
        # caller_region (derived from caller-ID in agent.py deps) powers an
        # automatic second attempt when the tenant-region verdict is
        # unhelpful — up to 1.5s extra on that rare path only.
        try:
            result, region_used = await validate_address_with_region_fallback(
                tenant_id=deps.get("tenant_id"),
                call_id=deps.get("call_id"),
                region_code=region_code,
                caller_region=deps.get("caller_region"),
                address_lines=address_lines,
                postal_code=postal_code or None,
                locality=city or None,
                supabase=deps.get("supabase"),
                timeout_seconds=1.5,
            )
            if region_used != region_code:
                logger.info(
                    "[validate_address] validated with region=%s "
                    "(tenant region=%s) call=%s",
                    region_used, region_code, deps.get("call_id"),
                )
        except Exception as exc:  # noqa: BLE001 — tool must never raise
            logger.error("[validate_address] unexpected error: %s", exc)
            result = {"verdict": "error", "formatted_address": None}

        # Cache the full bounded result for reuse by book_appointment /
        # capture_lead (skips the second Google call when the address the
        # model passes them matches this input).
        deps["_validated_address"] = {
            "input": {
                "street": street,
                "unit": unit,
                "postal_code": postal_code,
                "city": city,
            },
            "result": result,
            "ts": time.time(),
        }

        verdict = result.get("verdict", "error")
        formatted = result.get("formatted_address")

        if verdict == "confirmed" and formatted:
            state = (
                f"STATE:address_ok speech={formatted}"
                " | DIRECTIVE:confirm the address back in ONE short sentence"
                " and continue with the next intake step."
            )
        elif verdict == "confirmed_with_changes" and formatted:
            state = (
                f"STATE:address_corrected speech={formatted}"
                " | DIRECTIVE:read the corrected address once, ask briefly if"
                " that's right. If the caller corrects, call validate_address"
                " again with the corrected pieces."
            )
        elif verdict == "unconfirmed":
            missing = _missing_component_hint(result, postal_code)
            state = (
                f"STATE:address_unclear missing={missing}"
                " | DIRECTIVE:ask ONE targeted follow-up for the unclear"
                " piece, then call validate_address again. After one retry,"
                " proceed with what the caller said."
            )
        else:
            # skipped | unsupported_region | error — and the defensive case
            # of confirmed/corrected with no formatted_address. Never block,
            # never expose internals.
            state = (
                f"STATE:address_noted speech={as_given}"
                " | DIRECTIVE:read it back once and continue. Never mention"
                " validation."
            )

        deps["_last_tool_state"] = state
        return state

    return validate_address
