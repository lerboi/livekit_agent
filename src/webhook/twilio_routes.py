"""Twilio webhook routes.

All four POST endpoints are signature-gated via a router-level dependency
(see src/webhook/security.py). Form data is read once in the dependency and
stashed on request.state.form_data for handlers.

Phase 40: /twilio/incoming-call performs live routing — tenant lookup ->
subscription check (fail-open) -> evaluate_schedule -> check_outbound_cap
-> correct TwiML (AI SIP or owner-pickup parallel-ring Dial).

The other three endpoints return empty TwiML in Phase 39 — Phase 40 wires
the real dial-status duration writeback and SMS forwarding logic.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response

from src.lib.phone import _normalize_phone
from .schedule import ScheduleDecision, evaluate_schedule
from .caps import check_outbound_cap
from .security import verify_twilio_signature

logger = logging.getLogger("voco-webhook")

# Subscription statuses that block inbound calls (copied from agent.py:52)
BLOCKED_STATUSES = ["canceled", "paused", "incomplete"]


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


def _owner_pickup_twiml(caller: str, pickup_numbers: list[str], timeout: int) -> str:
    """Build parallel-ring Dial TwiML for owner-pickup routing.

    Dials up to 5 numbers simultaneously. The action URL points to
    /twilio/dial-status so the dial result can be processed.
    """
    base = os.environ.get("RAILWAY_WEBHOOK_URL", "")
    action_url = f"{base}/twilio/dial-status"
    number_elements = "".join(f"<Number>{n}</Number>" for n in pickup_numbers[:5])
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Response><Dial timeout="{timeout}" callerId="{caller}" '
        f'action="{action_url}">{number_elements}</Dial></Response>'
    )


async def _insert_owner_pickup_call(
    tenant_id: str, call_sid: str, from_number: str, to_number: str,
) -> None:
    """Insert a calls row for owner-pickup routing BEFORE returning TwiML.

    Records tenant_id, call_sid, from/to numbers, and routing_mode so the
    dial-status callback can link back to this call.
    """
    from src.supabase_client import get_supabase_admin

    def _insert():
        return get_supabase_admin().table("calls").insert({
            "tenant_id": tenant_id,
            "call_sid": call_sid,
            "from_number": from_number,
            "to_number": to_number,
            "routing_mode": "owner_pickup",
        }).execute()

    await asyncio.to_thread(_insert)


# --- /twilio/incoming-call ---------------------------------------------------
#
# Phase 40 live routing composition (D-02):
# tenant lookup -> subscription check (fail-open) -> evaluate_schedule ->
# check_outbound_cap -> correct TwiML (AI SIP or owner-pickup Dial)


@router.post("/incoming-call")
async def incoming_call(request: Request) -> Response:
    """Twilio voice webhook — return TwiML telling Twilio how to handle the call.

    Phase 40: live routing logic — tenant lookup, subscription check,
    schedule evaluation, cap check, parallel-ring TwiML or AI SIP TwiML.
    """
    form_data = request.state.form_data  # set by verify_twilio_signature
    to_raw = form_data.get("To", "")
    from_raw = form_data.get("From", "")
    call_sid = form_data.get("CallSid", "")
    to_number = _normalize_phone(to_raw)
    from_number = _normalize_phone(from_raw)

    logger.info(
        "[webhook] /twilio/incoming-call from=%s to=%s sid=%s",
        from_number, to_number, call_sid,
    )

    # 1. Tenant lookup (fail-open: if DB down, fall through to AI)
    tenant = None
    try:
        from src.supabase_client import get_supabase_admin

        def _query():
            supabase = get_supabase_admin()
            return (
                supabase.table("tenants")
                .select(
                    "id, call_forwarding_schedule, tenant_timezone, country, "
                    "pickup_numbers, dial_timeout_seconds, subscriptions(status)"
                )
                .eq("phone_number", to_number)
                .limit(1)
                .execute()
            )

        response = await asyncio.to_thread(_query)
        rows = response.data or []
        if rows:
            tenant = rows[0]
            logger.info("[webhook] Tenant lookup hit: id=%s", tenant.get("id"))
        else:
            logger.info("[webhook] Tenant lookup miss for to=%s", to_number)
    except Exception as e:
        logger.warning("[webhook] Tenant lookup failed (fail-open): %s", e)

    # No tenant found -> AI TwiML (fail-open)
    if not tenant:
        return _xml_response(_ai_sip_twiml())

    # 2. Subscription check (fail-open: if parsing fails, continue)
    try:
        sub_rows = tenant.get("subscriptions") or []
        status = sub_rows[0]["status"] if sub_rows else None
        if status in BLOCKED_STATUSES:
            logger.info(
                "[webhook] Blocked subscription (%s) for tenant %s — AI TwiML",
                status, tenant["id"],
            )
            return _xml_response(_ai_sip_twiml())
    except Exception as e:
        logger.warning("[webhook] Subscription check failed (fail-open): %s", e)

    # 3. Evaluate schedule
    decision = evaluate_schedule(
        tenant.get("call_forwarding_schedule", {}),
        tenant.get("tenant_timezone", "UTC"),
        datetime.now(tz=timezone.utc),
    )

    # 4. Cap check for owner_pickup
    if decision.mode == "owner_pickup":
        try:
            under_cap = await check_outbound_cap(
                tenant["id"], tenant.get("country", "US"),
            )
            if not under_cap:
                decision = ScheduleDecision(mode="ai", reason="soft_cap_hit")
                logger.warning(
                    "[webhook] Cap breach for tenant %s — downgrading to AI",
                    tenant["id"],
                )
        except Exception as e:
            logger.warning("[webhook] Cap check failed (fail-open): %s", e)

    # 5. Build TwiML based on decision
    if decision.mode == "owner_pickup":
        pickup_numbers = [
            p["number"]
            for p in (tenant.get("pickup_numbers") or [])
            if p.get("number")
        ]
        if not pickup_numbers:
            logger.info(
                "[webhook] No pickup numbers for tenant %s — AI TwiML",
                tenant["id"],
            )
            return _xml_response(_ai_sip_twiml())

        # Insert calls row BEFORE returning TwiML
        try:
            await _insert_owner_pickup_call(
                tenant["id"], call_sid, from_number, to_number,
            )
        except Exception as e:
            logger.warning("[webhook] Failed to insert calls row: %s", e)

        timeout = tenant.get("dial_timeout_seconds", 15)
        return _xml_response(
            _owner_pickup_twiml(from_number, pickup_numbers, timeout),
        )

    # Default: AI TwiML
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
