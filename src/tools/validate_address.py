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

import difflib
import logging
import os
import re
import time

from livekit.agents import function_tool, RunContext

from ..integrations.google_maps import validate_address_with_region_fallback
from ..integrations.onemap import is_sg_postal, lookup_postal, normalize_postal
from ..lib.service_area import classify_service_area

logger = logging.getLogger(__name__)

# P1.4 (2026-09-04): Singapore postal-first resolution via OneMap. When the
# tenant is SG and the caller gave a 6-digit postal code, resolve the building
# from the postal (which STT transcribes reliably) BEFORE — instead of — the
# Google call, and fall through to the unchanged STATE logic. `false` reverts
# to the Google-only path without a deploy.
ONEMAP_ENABLED = os.environ.get("VOCO_SG_ONEMAP", "true").strip().lower() != "false"

_ROAD_ABBREVIATIONS = {
    "rd": "road", "st": "street", "ave": "avenue", "av": "avenue",
    "dr": "drive", "cres": "crescent", "cl": "close", "ln": "lane",
    "blvd": "boulevard", "pl": "place", "ter": "terrace", "wy": "way",
    "hwy": "highway", "ctr": "central", "ctrl": "central", "jln": "jalan",
    "lor": "lorong", "blk": "block",
}
_ROAD_FUZZY_THRESHOLD = 0.8


def _norm_words(value: str | None) -> list[str]:
    """Casefold, strip punctuation, expand common road abbreviations."""
    words = re.findall(r"[a-z0-9]+", (value or "").casefold())
    return [_ROAD_ABBREVIATIONS.get(w, w) for w in words]


def _spoken_matches_onemap(street: str, blk_no: str, road_name: str) -> bool:
    """Does the caller's spoken street already agree with the OneMap
    building? True when it contains the block number, or the road name
    fuzzy-matches (>= 0.8 ratio over the alphabetic words). 'Kenboro Drive'
    vs 'Canberra Drive' fails both → confirmed_with_changes → the agent reads
    the corrected form back once (the existing address_corrected branch)."""
    spoken = _norm_words(street)
    if not spoken:
        return False
    blk = (blk_no or "").strip().casefold()
    if blk and blk in spoken:
        return True
    road = [w for w in _norm_words(road_name) if not w.isdigit()]
    spoken_alpha = [w for w in spoken if not w.isdigit()]
    if not road or not spoken_alpha:
        return False
    ratio = difflib.SequenceMatcher(None, " ".join(road), " ".join(spoken_alpha)).ratio()
    return ratio >= _ROAD_FUZZY_THRESHOLD


def _onemap_result(hit: dict, *, street: str, unit: str) -> dict:
    """Shape a OneMap hit exactly like google_maps._voco_result so every
    downstream consumer (STATE building, deps cache, book_appointment /
    capture_lead reuse, service-area gate) works unchanged."""
    blk_no = str(hit.get("BLK_NO") or "").strip()
    road_name = str(hit.get("ROAD_NAME") or "").strip().title()
    building = str(hit.get("BUILDING") or "").strip()
    postal = str(hit.get("POSTAL") or "").strip()
    if building.upper() == "NIL":
        building = ""
    else:
        building = building.title()

    formatted = f"Block {blk_no} {road_name}".strip() if blk_no else road_name
    if building:
        formatted = f"{formatted}, {building}"

    verdict = (
        "confirmed"
        if _spoken_matches_onemap(street, blk_no, hit.get("ROAD_NAME") or "")
        else "confirmed_with_changes"
    )

    def _float(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    return {
        "verdict": verdict,
        "formatted_address": formatted,
        "place_id": None,
        "latitude": _float(hit.get("LATITUDE")),
        "longitude": _float(hit.get("LONGITUDE")),
        "address_components": {
            "street_number": blk_no or None,
            "route": road_name or None,
            "subpremise": unit or None,
            "locality": "Singapore",
            "admin_area_level_1": None,
            "admin_area_level_2": None,
            "postal_code": postal or None,
            "country": "Singapore",
            "country_code": "SG",
        },
        "latency_ms": 0,
        "raw_status": 200,
    }


# M16 P1 (Capability A) — caller-facing wording guard for the out-of-area
# paths. Decision (e): the gate's internals stay silent — the caller hears only
# "a bit outside the area we usually cover", never any implementation jargon.
_OOA_PROHIBITED = (
    ' Never say "zone", "service area", "coverage", "buffer", or "travel time"'
    ' — say only that it is "a bit outside the area we usually cover".'
)


def _out_of_area_state(
    action: str, formatted: str | None, referral_note: str | None
) -> str:
    """Build the STATE+DIRECTIVE for a confirmed out-of-area address, branched
    on the owner's out_of_area_action. All three branches keep the lead (the
    owner's #1 fear is a lost lead); they differ only in what the AI offers.

    callback (default) — do not book; take a message + promise a call-back.
    decline_referral   — do not book; politely decline + optional referral.
    trip_fee           — proceed to book; warn an extra travel charge may apply.
    """
    speech = formatted or ""
    if action == "decline_referral":
        directive = (
            "the address is confirmed but outside the area the team can get to."
            " Do NOT offer or book a time. In ONE polite sentence, let the caller"
            " know it's outside the area the team usually covers"
        )
        if referral_note:
            directive += f', and suggest they try: "{referral_note}"'
        directive += (
            ". Still collect the caller's name, a callback number, and the job,"
            " then call capture_lead so the owner keeps the lead."
        )
    elif action == "trip_fee":
        directive = (
            "the address is confirmed but a little outside the usual area. You can"
            " still book it. In ONE sentence, gently mention it's a bit outside the"
            " usual area so there may be an extra travel charge the owner will"
            " confirm, then continue to check availability and book as normal."
        )
    else:  # callback (default) — also the fallback for any unexpected value
        directive = (
            "the address is confirmed but a little outside the area the team usually"
            " covers. Do NOT offer or book a time. In ONE warm sentence, let the"
            " caller know it's a bit outside the usual area, so you'll take their"
            " details and have the team call back to confirm they can get out there."
            " Then collect the caller's name, a callback number, and the job, and"
            " call capture_lead."
        )
    return (
        f"STATE:address_out_of_area action={action} speech={speech}"
        f" | DIRECTIVE:{directive}{_OOA_PROHIBITED}"
    )


_SCHEMA = {
    "name": "validate_address",
    "description": (
        "Validate the service address the moment the caller finishes giving "
        "it — do not wait for booking. Pass the pieces exactly as the caller "
        "said them. The return tells you whether the address came back "
        "confirmed, corrected, or unclear, and exactly what to say next. "
        "The caller's word always beats the validated form — if they correct "
        "any part of it, call this tool again with their correction. Speak a "
        "one-sentence filler first ('Let me just check that address…'), then "
        "invoke in the same turn. This tool's return is a state+directive "
        "string — data for you, not to be read aloud."
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


# P1.3 (2026-09-04) address-loop cap. The Nth `unconfirmed` verdict for the
# SAME normalized street (N = UNCONFIRMED_RETRY_CAP → the first retry is the
# last), or the Mth unconfirmed verdict in the call regardless of street
# (M = UNCONFIRMED_TOTAL_CAP → STT-drifted street strings can't reset the
# clock), returns STATE:address_noted instead of STATE:address_unclear. First
# attempt behaviour is unchanged.
UNCONFIRMED_RETRY_CAP = 2
UNCONFIRMED_TOTAL_CAP = 3


def get_cached_validation(deps: dict, street: str, postal_code: str) -> dict | None:
    """Return the cached bounded-validation result from an earlier
    validate_address call IF the input address matches; else None.

    Match rule: normalized street AND normalized postal code must be equal.
    Unit differences are deliberately tolerated (callers often add/refine the
    unit between the early validation and the booking commit; the unit does
    not change the Google verdict for the building).

    Postal tolerance (2026-06-11, findings.md P2): when the cached validation
    ran WITHOUT a caller-supplied postal code (the address_ok_confirm_postal
    flow — the lookup supplied one and the caller then confirmed it), a
    booking that passes that confirmed postal still matches: an empty cached
    postal matches when the requested postal equals the postal the lookup
    returned. Without this, confirming the suggested postal forced a second
    Google call at booking time.

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
    cached_postal = _norm(cached_input.get("postal_code"))
    requested_postal = _norm(postal_code)
    if cached_postal != requested_postal:
        if cached_postal:
            return None
        # Cached validation had no caller postal — accept when the requested
        # postal is exactly the one the lookup returned (caller confirmed it).
        result_postal = _norm(
            (result.get("address_components") or {}).get("postal_code")
        )
        if not result_postal or result_postal != requested_postal:
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

        # 2026-09-04 P1.3: count validation attempts per normalized street (and
        # in total) so the unconfirmed branch can enforce the "after one retry,
        # proceed with what the caller said" rule in CODE. The prose version
        # of that rule was not enough — under last-instruction-wins the
        # identical address_unclear return re-arrived and the model asked the
        # same question again (4× in 66s on the 2026-08-17 Canberra Drive
        # call). Keyed per street so a genuine caller correction earns one
        # fresh attempt; the total counter is the backstop when STT drifts the
        # street string between attempts.
        _attempts = deps.setdefault("_validate_attempts", {})
        _attempt_key = _norm(street) or "_"
        _attempts[_attempt_key] = _attempts.get(_attempt_key, 0) + 1
        _attempts["__total__"] = _attempts.get("__total__", 0) + 1

        # P1.4 (2026-09-04): Singapore postal-first. A 6-digit SG postal code
        # pins down one building, and STT gets digits right far more often
        # than proper-noun street names — resolve it via OneMap before (and
        # instead of) Google. On a hit, `result` takes the exact google_maps
        # shape and the STATE logic below runs unchanged; on a miss / error /
        # flag off, today's Google path runs exactly as before.
        result = None
        if (
            ONEMAP_ENABLED
            and region_code == "SG"
            and is_sg_postal(postal_code)
        ):
            try:
                _hit = await lookup_postal(normalize_postal(postal_code))
            except Exception as exc:  # noqa: BLE001 — lookup is never-raising; belt and braces
                logger.warning("[validate_address] onemap lookup raised: %s", exc)
                _hit = None
            if _hit:
                result = _onemap_result(_hit, street=street, unit=unit)
                logger.info(
                    "[validate_address] onemap hit postal=%s blk=%s road=%s "
                    "building=%s verdict=%s call=%s",
                    normalize_postal(postal_code), _hit.get("BLK_NO"),
                    _hit.get("ROAD_NAME"), _hit.get("BUILDING"),
                    result["verdict"], deps.get("call_id"),
                )

        # The fallback orchestrator is contractually never-raising, but this
        # tool must ALSO never raise (an exception here would surface as a
        # failed tool call mid-conversation) — belt and braces.
        # caller_region (derived from caller-ID in agent.py deps) powers an
        # automatic second attempt when the tenant-region verdict is
        # unhelpful — up to 1.5s extra on that rare path only.
        try:
            if result is not None:
                region_used = region_code
            else:
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
        # 2026-06-11 (findings.md P2): a postal code in the result that the
        # caller never spoke is a LOOKUP-SUPPLIED value (incident call
        # 31559053: Google inferred postal 752106, the agent asserted it and
        # then argued with the caller's correction). Surface it as its own
        # STATE so the prompt's confirm-as-a-question rule applies.
        looked_up_postal = (result.get("address_components") or {}).get("postal_code")

        if verdict == "confirmed" and formatted and not postal_code and looked_up_postal:
            state = (
                f"STATE:address_ok_confirm_postal speech={formatted}"
                f" postal={looked_up_postal}"
                " | DIRECTIVE:confirm the address in ONE short sentence and ask"
                " whether the postal code is right as a QUESTION, digit by digit"
                " — never state it as a fact. If the caller gives a different"
                " one, theirs is correct: call validate_address again with it."
            )
        elif verdict == "confirmed" and formatted:
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
        elif verdict == "unconfirmed" and (
            _attempts[_attempt_key] >= UNCONFIRMED_RETRY_CAP
            or _attempts["__total__"] >= UNCONFIRMED_TOTAL_CAP
        ):
            # P1.3 cap: the retry was already spent (same street asked twice,
            # or three unconfirmed attempts in the call however the street was
            # transcribed). Stop asking; carry the caller's words forward.
            logger.info(
                "[validate_address] unconfirmed cap reached (street=%d total=%d) call=%s",
                _attempts[_attempt_key], _attempts["__total__"], deps.get("call_id"),
            )
            state = (
                f"STATE:address_noted speech={as_given}"
                " | DIRECTIVE:read it back once in the caller's words and"
                " continue with the next intake step. Do not ask about the"
                " address again this call. Never mention validation."
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

        # ── Service-Area gate (M16 P1, Capability A) ────────────────────────
        # Does the tenant serve this address at all? Classify ONLY on a solidly
        # confirmed address (the Google-normalized postal + town are trusted),
        # and skip the confirm-postal branch where the postal is an unconfirmed
        # lookup — defer to the re-validate once the caller confirms it. The
        # classification is stashed on deps for capture_lead + the post-call
        # owner notification; an 'out_of_area' verdict overrides the directive
        # per the owner's chosen action. Bias is to false-ACCEPT — see
        # service_area.classify_service_area. Never blocks the call path.
        deps["_service_area"] = {"verdict": "unknown", "matched_on": None}
        _confirm_postal_branch = (
            verdict == "confirmed" and not postal_code and looked_up_postal
        )
        if (
            verdict in ("confirmed", "confirmed_with_changes")
            and formatted
            and not _confirm_postal_branch
        ):
            try:
                _components = result.get("address_components") or {}
                _sa = classify_service_area(
                    zones=(deps.get("_slot_cache") or {}).get("service_zones") or [],
                    postal_code=_components.get("postal_code"),
                    locality=_components.get("locality"),
                )
                deps["_service_area"] = _sa
                if _sa["verdict"] == "out_of_area":
                    _tenant = deps.get("tenant") or {}
                    state = _out_of_area_state(
                        _tenant.get("out_of_area_action") or "callback",
                        formatted,
                        _tenant.get("out_of_area_referral_note"),
                    )
                    logger.info(
                        "[validate_address] out-of-area (action=%s) call=%s",
                        _tenant.get("out_of_area_action") or "callback",
                        deps.get("call_id"),
                    )
            except Exception as exc:  # noqa: BLE001 — gate must never break the call
                logger.warning("[validate_address] service-area gate failed open: %s", exc)

        deps["_last_tool_state"] = state
        return state

    return validate_address
