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
from datetime import datetime
from zoneinfo import ZoneInfo

import sentry_sdk

sentry_sdk.init(
    dsn=os.environ.get("SENTRY_DSN"),
    traces_sample_rate=0.1,
    environment=os.environ.get("PYTHON_ENV", "production"),
)

from google.genai import types as genai_types
from livekit.agents import AgentSession, Agent, cli, JobContext, WorkerOptions, room_io
from livekit.plugins import google, noise_cancellation
from livekit import api, rtc

from .prompt import build_system_prompt
from .tools import create_tools
from .supabase_client import get_supabase_admin
from .post_call import run_post_call_pipeline
from .health import start_health_server

logger = logging.getLogger("voco-agent")

# Voice mapping: tone_preset -> Gemini voice name
VOICE_MAP = {
    "professional": "Zephyr",
    "friendly": "Aoede",
    "local_expert": "Achird",
}

# Subscription statuses that block inbound calls
BLOCKED_STATUSES = ["canceled", "paused", "incomplete"]

# Timeout for SIP participant to join the room (seconds)
PARTICIPANT_TIMEOUT_S = 30


class VocoAgent(Agent):
    """Voco AI receptionist agent with dynamic tools."""

    def __init__(self, instructions: str, tools: list):
        super().__init__(instructions=instructions, tools=tools)

    async def on_enter(self) -> None:
        # Greeting is handled at the entrypoint level after session.start()
        pass


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
            tenant_resp = await asyncio.to_thread(
                lambda: supabase.table("tenants")
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
        country = tenant.get("country", "US") if tenant else "US"

        logger.info(f"[agent] Tenant: {tenant_id or 'NONE'} ({business_name})")

        # ── Parallel DB queries (subscription + intake questions + call record) ──
        # All depend on tenant_id but NOT on each other — run concurrently to minimize latency
        start_timestamp = int(time.time() * 1000)
        sub_result = None
        intake_questions = ""
        call_record = None

        if tenant_id:
            sub_task = asyncio.to_thread(
                lambda: supabase.table("subscriptions")
                .select("status")
                .eq("tenant_id", tenant_id)
                .eq("is_current", True)
                .limit(1)
                .execute()
            )
            intake_task = asyncio.to_thread(
                lambda: supabase.table("services")
                .select("intake_questions")
                .eq("tenant_id", tenant_id)
                .eq("is_active", True)
                .execute()
            )
            call_task = asyncio.to_thread(
                lambda: supabase.table("calls")
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
                .execute()
            )

            results = await asyncio.gather(sub_task, intake_task, call_task, return_exceptions=True)

            # Process subscription result
            if not isinstance(results[0], Exception):
                sub_data = results[0].data
                sub = sub_data[0] if sub_data else None
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
            else:
                logger.warning(f"[agent] Subscription check failed (allowing call): {results[0]}")

            # Process intake questions result
            if not isinstance(results[1], Exception) and results[1].data:
                all_questions = []
                for s in results[1].data:
                    for q in (s.get("intake_questions") or []):
                        if q not in all_questions:
                            all_questions.append(q)
                intake_questions = "\n".join(all_questions)

            # Process call record result
            if not isinstance(results[2], Exception):
                call_record = results[2].data[0] if results[2].data else None
            else:
                logger.error(f"[agent] Call record insert failed: {results[2]}")
        else:
            logger.warning(f"[agent] No tenant for {to_number} — skipping call record (tenant_id is NOT NULL)")

        # ── Build system prompt ──
        system_prompt = build_system_prompt(
            locale,
            business_name=business_name,
            onboarding_complete=onboarding_complete,
            tone_preset=tone_preset,
            intake_questions=intake_questions,
            country=country,
        )
        # Inject current date so the AI can map day names to YYYY-MM-DD dates
        local_now = datetime.now(tz=ZoneInfo(tenant_timezone))
        system_prompt += f"\n\nToday is {local_now.strftime('%A, %B %d, %Y')}."

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
            # Minimal thinking for lowest latency
            thinking_config=genai_types.ThinkingConfig(
                thinking_level="minimal",
                include_thoughts=False,
            ),
            # Use Gemini's native server VAD (handles echo cancellation internally)
            # Do NOT disable it — client-side VAD causes commit_audio errors and self-interruption
        )

        agent = VocoAgent(instructions=system_prompt, tools=tools)

        # Use default AgentSession — Gemini's native VAD handles turn detection
        session = AgentSession(llm=model)

        # ── Collect transcript in real-time ──
        transcript_turns = []

        @session.on("conversation_item_added")
        def on_conversation_item(event):
            text = getattr(event.item, "text_content", None)
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
            logger.error(f"[agent] Session error: room={call_id} tenant={tenant_id} error={event.error}")
            actual_error = getattr(event.error, "error", event.error)
            sentry_sdk.capture_exception(actual_error)

        # ── Handle session end (post-call pipeline) — registered BEFORE start to avoid race ──
        egress_id = None
        recording_path = f"{call_id}.mp4"

        async def _on_close_async():
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

        @session.on("close")
        def on_close(event):
            asyncio.create_task(_on_close_async())

        # ── Start session (with SIP-optimized noise cancellation) ──
        await session.start(
            agent=agent,
            room=ctx.room,
            room_options=room_io.RoomOptions(
                audio_input=room_io.AudioInputOptions(
                    noise_cancellation=lambda params: (
                        noise_cancellation.BVCTelephony()
                        if params.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
                        else noise_cancellation.BVC()
                    ),
                ),
            ),
        )
        logger.info(f"[agent] Session started: room={call_id}")

        # ── Start Egress recording (before greeting so recording captures it) ──
        async def _start_egress():
            nonlocal egress_id
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
                    await asyncio.to_thread(
                        lambda: supabase.table("calls").update({"egress_id": egress_id}).eq("call_id", call_id).execute()
                    )
            except Exception as e:
                logger.error(f"[agent] Failed to start egress: {e}")

        asyncio.create_task(_start_egress())

        # ── Generate greeting (non-blocking — speech plays in background) ──
        session.generate_reply(
            instructions="Greet the caller now.",
        )

    except Exception as e:
        logger.error(f"[agent] Entry function error: {e}", exc_info=True)
        sentry_sdk.capture_exception(e)
        raise


if __name__ == "__main__":
    start_health_server()
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="voco-voice-agent",
        )
    )
