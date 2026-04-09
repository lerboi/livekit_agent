"""Twilio webhook routes.

All four POST endpoints are signature-gated via a router-level dependency
(see src/webhook/security.py). Form data is read once in the dependency and
stashed on request.state.form_data for handlers.

Phase 39 scope (D-13): /twilio/incoming-call performs a real tenant lookup
using _normalize_phone(To) so the full wiring path (signature -> form parse
-> tenant lookup -> TwiML render) is exercised, but the handler ALWAYS
returns a hardcoded AI TwiML <Dial><Sip> branch regardless of lookup result.
Phase 40 replaces the hardcoded branch with evaluate_schedule +
check_outbound_cap composition.

The other three endpoints return empty TwiML in Phase 39 — Phase 40 wires
the real dial-status duration writeback and SMS forwarding logic.
"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, Depends, Request, Response

from src.lib.phone import _normalize_phone
from .security import verify_twilio_signature

logger = logging.getLogger("voco-webhook")


router = APIRouter(
    prefix="/twilio",
    dependencies=[Depends(verify_twilio_signature)],
)


# --- TwiML helpers -----------------------------------------------------------


def _ai_sip_twiml() -> str:
    """Build the hardcoded AI TwiML response.

    Dials the existing LiveKit SIP URI from env var LIVEKIT_SIP_URI. In Phase
    39 this value is not critical because no production Twilio number is
    reconfigured to call this webhook — the default is a placeholder.
    """
    sip_uri = os.environ.get("LIVEKIT_SIP_URI", "sip:voco@sip.livekit.cloud")
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<Response><Dial><Sip>{sip_uri}</Sip></Dial></Response>"
    )


def _empty_twiml() -> str:
    """Empty TwiML response — <Response/>."""
    return '<?xml version="1.0" encoding="UTF-8"?>\n<Response/>'


def _xml_response(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


# --- /twilio/incoming-call ---------------------------------------------------
#
# D-13: Phase 39 performs a tenant lookup by the To number (same pattern as
# src/agent.py entrypoint), but always returns the hardcoded AI TwiML branch.
# Phase 40 replaces the hardcoded branch with evaluate_schedule +
# check_outbound_cap composition — one-line diff.


@router.post("/incoming-call")
async def incoming_call(request: Request) -> Response:
    """Twilio voice webhook — return TwiML telling Twilio how to handle the call.

    Phase 39 (D-13): always returns AI TwiML after exercising the full wiring
    path (signature -> form parse -> tenant lookup -> TwiML render). Phase 40
    replaces the hardcoded branch with live routing logic.
    """
    form_data = request.state.form_data  # set by verify_twilio_signature
    to_raw = form_data.get("To", "")
    from_raw = form_data.get("From", "")
    to_number = _normalize_phone(to_raw)
    from_number = _normalize_phone(from_raw)

    logger.info(
        f"[webhook] /twilio/incoming-call from={from_number} to={to_number}"
    )

    # D-13 dead-weight tenant lookup — exercises the Supabase code path so
    # Phase 40's diff is minimal. Result is logged but not used.
    try:
        from src.supabase_client import get_supabase_admin

        def _query():
            supabase = get_supabase_admin()
            return (
                supabase.table("tenants")
                .select("id, call_forwarding_schedule, tenant_timezone, country")
                .eq("phone_number", to_number)
                .limit(1)
                .execute()
            )

        response = await asyncio.to_thread(_query)
        rows = response.data or []
        if rows:
            logger.info(f"[webhook] Tenant lookup hit: id={rows[0].get('id')}")
        else:
            logger.info(f"[webhook] Tenant lookup miss for to={to_number}")
    except Exception as e:
        # Fail-open: webhook still returns TwiML even if DB is down
        logger.warning(f"[webhook] Tenant lookup failed (fail-open): {e}")

    # Phase 39: always return the hardcoded AI TwiML branch (D-13)
    return _xml_response(_ai_sip_twiml())


@router.post("/dial-status")
async def dial_status(request: Request) -> Response:
    """Twilio dial-status callback (Phase 40 wires duration writeback).

    Phase 39 returns empty TwiML so the endpoint exists and the signature path
    is exercised. Phase 40 reads CallStatus + DialCallDuration from
    request.state.form_data and updates calls.outbound_dial_duration_sec.
    """
    return _xml_response(_empty_twiml())


@router.post("/dial-fallback")
async def dial_fallback(request: Request) -> Response:
    """Twilio dial-fallback (invoked if primary dial fails — Phase 40).

    Phase 39 returns empty TwiML. Phase 40 returns an AI TwiML branch here
    so the fallback always lands on the AI.
    """
    return _xml_response(_empty_twiml())


@router.post("/incoming-sms")
async def incoming_sms(request: Request) -> Response:
    """Twilio SMS webhook (Phase 40 wires forwarding to pickup_numbers with sms_forward=true).

    Phase 39 returns empty TwiML so inbound SMS is acknowledged but not
    forwarded. No sms_messages table insert (Phase 40).
    """
    return _xml_response(_empty_twiml())
