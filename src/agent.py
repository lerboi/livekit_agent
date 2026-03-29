"""
Voco LiveKit Voice Agent

Main entry point for the AI receptionist agent.
  Twilio SIP -> LiveKit -> Gemini 3.1 Flash Live (native audio-to-audio)

Architecture:
- Each inbound call creates a LiveKit room via SIP dispatch rule
- This agent joins the room, looks up the tenant, and opens a Gemini Live session
- All 6 tools execute in-process (no webhook round-trips)
- Post-call pipeline runs immediately when the session closes
"""

import os
import json
import time
import asyncio
import logging

import sentry_sdk

sentry_sdk.init(
    dsn=os.environ.get("SENTRY_DSN"),
    traces_sample_rate=0.1,
    environment=os.environ.get("PYTHON_ENV", "production"),
)

from livekit.agents import AgentSession, Agent, cli, JobContext
from livekit.plugins import google
from livekit import api

from .prompt import build_system_prompt
from .tools import create_tools
from .supabase_client import get_supabase_admin
from .post_call import run_post_call_pipeline
from .utils import calculate_initial_slots
from .health import start_health_server

logger = logging.getLogger("voco-agent")

# Voice mapping: tone_preset -> Gemini voice name
VOICE_MAP = {
    "professional": "Kore",
    "friendly": "Aoede",
    "local_expert": "Achird",
}

# Subscription statuses that block inbound calls
BLOCKED_STATUSES = ["canceled", "paused", "incomplete"]

# Timeout for SIP participant to join the room (seconds)
PARTICIPANT_TIMEOUT_S = 30


class VocoAgent(Agent):
    """Voco AI receptionist agent with dynamic tools."""

    def __init__(self, system_prompt: str, tools: list):
        super().__init__(instructions=system_prompt, tools=tools)


# Start health check server (non-blocking, separate port)
start_health_server()


async def entrypoint(ctx: JobContext):
    try:
        # ── Connect to room ──
        await ctx.connect()
        call_id = ctx.room.name
        logger.info(f"[agent] Connected to room: {call_id}")

        # ── Wait for SIP participant (with timeout) ──
        participant = await asyncio.wait_for(
            ctx.wait_for_participant(),
            timeout=PARTICIPANT_TIMEOUT_S,
        )

        # Extract phone numbers from SIP participant attributes
        # sip.trunkPhoneNumber = the Twilio number being called (used for tenant lookup)
        # sip.phoneNumber = the caller's number
        attrs = participant.attributes or {}
        to_number = attrs.get("sip.trunkPhoneNumber") or attrs.get("sip.to") or ""
        from_number = attrs.get("sip.phoneNumber") or attrs.get("sip.from") or ""
        sip_participant_identity = participant.identity or ""

        # Check if this is a test call (metadata set by test-call route)
        is_test_call = False
        try:
            room_meta = json.loads(ctx.room.metadata) if ctx.room.metadata else {}
            is_test_call = room_meta.get("test_call") is True
        except Exception:
            pass

        logger.info(f"[agent] Call started: room={call_id} from={from_number} to={to_number} test={is_test_call}")

        # ── Tenant lookup ──
        supabase = get_supabase_admin()
        try:
            tenant_resp = (
                supabase.table("tenants")
                .select("*")
                .eq("phone_number", to_number)
                .single()
                .execute()
            )
            tenant = tenant_resp.data
        except Exception as e:
            logger.warning(f"[agent] Tenant lookup failed for {to_number}: {e}")
            tenant = None

        tenant_id = tenant.get("id") if tenant else None
        onboarding_complete = tenant.get("onboarding_complete", False) if tenant else False
        business_name = tenant.get("business_name", "Voco") if tenant else "Voco"
        locale = tenant.get("default_locale", "en") if tenant else "en"
        tone_preset = tenant.get("tone_preset", "professional") if tenant else "professional"
        owner_phone = tenant.get("owner_phone") if tenant else None
        tenant_timezone = tenant.get("tenant_timezone", "America/Chicago") if tenant else "America/Chicago"

        logger.info(f"[agent] Tenant: {tenant_id or 'NONE'} ({business_name})")

        # ── Subscription gate (fail-open) ──
        if tenant_id:
            try:
                sub_resp = (
                    supabase.table("subscriptions")
                    .select("status")
                    .eq("tenant_id", tenant_id)
                    .eq("is_current", True)
                    .limit(1)
                    .execute()
                )
                sub = sub_resp.data[0] if sub_resp.data else None

                if sub and sub.get("status") in BLOCKED_STATUSES:
                    logger.info(f"[agent] Subscription blocked: tenant={tenant_id} status={sub['status']} — disconnecting caller")
                    try:
                        lk = api.LiveKitAPI()
                        await lk.room.remove_participant(
                            api.RoomParticipantIdentity(room=call_id, identity=sip_participant_identity)
                        )
                        await lk.aclose()
                    except Exception as e:
                        logger.error(f"[agent] Failed to disconnect blocked caller: {e}")
                    return
            except Exception as e:
                logger.warning(f"[agent] Subscription check failed (allowing call): {e}")

        # ── Calculate available slots ──
        available_slots = ""
        if onboarding_complete and tenant_id:
            try:
                available_slots = calculate_initial_slots(supabase, tenant)
            except Exception as e:
                logger.error(f"[agent] Slot calculation failed: {e}")

        # ── Fetch intake questions ──
        intake_questions = ""
        if tenant_id:
            try:
                services_resp = (
                    supabase.table("services")
                    .select("intake_questions")
                    .eq("tenant_id", tenant_id)
                    .eq("is_active", True)
                    .execute()
                )
                if services_resp.data:
                    all_questions = []
                    for s in services_resp.data:
                        for q in (s.get("intake_questions") or []):
                            if q not in all_questions:
                                all_questions.append(q)
                    intake_questions = "\n".join(all_questions)
            except Exception:
                pass

        # ── Build system prompt ──
        system_prompt = build_system_prompt(locale, {
            "business_name": business_name,
            "onboarding_complete": onboarding_complete,
            "tone_preset": tone_preset,
            "intake_questions": intake_questions,
        })
        if available_slots:
            system_prompt += f"\n\nAVAILABLE APPOINTMENT SLOTS:\n{available_slots}"

        # ── Create call record (only when tenant exists — calls.tenant_id is NOT NULL) ──
        start_timestamp = int(time.time() * 1000)
        call_record = None

        if tenant_id:
            try:
                call_resp = (
                    supabase.table("calls")
                    .upsert(
                        {
                            "call_id": call_id,
                            "tenant_id": tenant_id,
                            "from_number": from_number,
                            "to_number": to_number,
                            "direction": "inbound",
                            "status": "started",
                            "start_timestamp": start_timestamp,
                            "call_provider": "livekit",
                        },
                        on_conflict="call_id",
                    )
                    .select("id")
                    .execute()
                )
                call_record = call_resp.data[0] if call_resp.data else None
            except Exception as e:
                logger.error(f"[agent] Call record insert failed: {e}")
        else:
            logger.warning(f"[agent] No tenant for {to_number} — skipping call record (tenant_id is NOT NULL)")

        # ── Create tools (in-process, direct Supabase access) ──
        tools = create_tools({
            "supabase": supabase,
            "tenant": tenant,
            "tenant_id": tenant_id,
            "call_id": call_id,
            "call_uuid": call_record.get("id") if call_record else None,
            "from_number": from_number,
            "to_number": to_number,
            "owner_phone": owner_phone,
            "start_timestamp": start_timestamp,
            "onboarding_complete": onboarding_complete,
            "tenant_timezone": tenant_timezone,
            "room_name": call_id,
            "sip_participant_identity": sip_participant_identity,
            "ctx": ctx,
        })

        # ── Create Gemini model + agent + session ──
        voice_name = VOICE_MAP.get(tone_preset, "Kore")

        model = google.realtime.RealtimeModel(
            model="gemini-3.1-flash-live-preview",
            voice=voice_name,
            temperature=0.3,
            instructions=system_prompt,
        )

        agent = VocoAgent(system_prompt=system_prompt, tools=tools)

        session = AgentSession(llm=model)

        # ── Collect transcript in real-time ──
        transcript_turns = []

        @session.on("conversation_item_added")
        def on_conversation_item(event):
            text = getattr(event.item, "text_content", None) or getattr(event.item, "text", None) or getattr(event, "text", None)
            if text:
                role = "user" if getattr(event.item, "role", None) == "user" else "agent"
                transcript_turns.append({
                    "role": role,
                    "content": text,
                    "timestamp": int(time.time() * 1000),
                })

        # ── Session error handler ──
        @session.on("error")
        def on_error(event):
            logger.error(f"[agent] Session error: room={call_id} tenant={tenant_id}", exc_info=event.error)
            sentry_sdk.capture_exception(event.error, tags={"callId": call_id, "tenantId": tenant_id})

        # ── Start session ──
        await session.start(agent=agent, room=ctx.room)
        logger.info(f"[agent] Session started: room={call_id}")

        # ── Start Egress recording ──
        egress_id = None
        recording_path = f"{call_id}.mp4"
        try:
            lk = api.LiveKitAPI()
            egress_info = await lk.egress.start_room_composite_egress(
                api.RoomCompositeEgressRequest(
                    room_name=call_id,
                    audio_only=True,
                    file_outputs=[api.EncodedFileOutput(
                        filepath=recording_path,
                        s3=api.S3Upload(
                            access_key=os.environ.get("SUPABASE_S3_ACCESS_KEY", ""),
                            secret=os.environ.get("SUPABASE_S3_SECRET_KEY", ""),
                            bucket="call-recordings",
                            region=os.environ.get("SUPABASE_S3_REGION", "ap-northeast-1"),
                            endpoint=os.environ.get("SUPABASE_S3_ENDPOINT", ""),
                            force_path_style=True,
                        ),
                    )],
                )
            )
            egress_id = egress_info.egress_id
            await lk.aclose()
            logger.info(f"[agent] Egress started: {egress_id}")

            if call_record:
                supabase.table("calls").update({"egress_id": egress_id}).eq("call_id", call_id).execute()
        except Exception as e:
            logger.error(f"[agent] Failed to start egress: {e}")

        # ── Generate greeting ──
        session.generate_reply()

        # ── Handle session end (post-call pipeline) ──
        @session.on("close")
        async def on_close(event):
            end_timestamp = int(time.time() * 1000)
            duration_sec = round((end_timestamp - start_timestamp) / 1000)
            logger.info(f"[agent] Session closed: room={call_id} duration={duration_sec}s")

            # Stop egress
            if egress_id:
                try:
                    lk = api.LiveKitAPI()
                    await lk.egress.stop_egress(api.StopEgressRequest(egress_id=egress_id))
                    await lk.aclose()
                except Exception as e:
                    logger.error(f"[agent] Failed to stop egress: {e}")

            # Run post-call pipeline
            try:
                await run_post_call_pipeline({
                    "supabase": supabase,
                    "call_id": call_id,
                    "call_uuid": call_record.get("id") if call_record else None,
                    "tenant_id": tenant_id,
                    "tenant": tenant,
                    "from_number": from_number,
                    "to_number": to_number,
                    "start_timestamp": start_timestamp,
                    "end_timestamp": end_timestamp,
                    "transcript_turns": transcript_turns,
                    "recording_storage_path": recording_path if egress_id else None,
                    "is_test_call": is_test_call,
                })
            except Exception as e:
                logger.error(f"[agent] Post-call pipeline error: {e}")
                sentry_sdk.capture_exception(e, tags={"callId": call_id, "tenantId": tenant_id, "phase": "post-call"})

    except Exception as e:
        logger.error(f"[agent] Entry function error: {e}", exc_info=True)
        sentry_sdk.capture_exception(e)
        raise


if __name__ == "__main__":
    cli.run_app(
        entrypoint,
        agent_name="voco-voice-agent",
    )
