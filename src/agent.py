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
from .webhook import start_webhook_server
from .integrations.xero import fetch_xero_context_bounded
from .lib.customer_context import fetch_merged_customer_context_bounded
from .lib.phone import _normalize_phone

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

        # Log raw SIP attributes for debugging phone number format issues
        logger.info(f"[agent] SIP attrs: {json.dumps(attrs)}")

        # Check if this is a test call (metadata set by test-call route)
        is_test_call = False
        room_meta = {}
        try:
            room_meta = json.loads(ctx.room.metadata) if ctx.room.metadata else {}
            is_test_call = room_meta.get("test_call") is True
        except Exception:
            pass

        # For test calls, outbound SIP participant won't have sip.trunkPhoneNumber
        # set to the tenant's number. Use room metadata as fallback.
        if is_test_call and room_meta.get("to_number"):
            to_number = room_meta["to_number"]

        # Normalize phone numbers to E.164 for reliable tenant lookup.
        # LiveKit SIP attributes may include sip:/tel: prefixes or @domain suffixes.
        # _normalize_phone is imported from src/lib/phone.py (extracted in Plan 39-04
        # so that src/webhook/twilio_routes.py can reuse the same logic).
        to_number = _normalize_phone(to_number)
        from_number = _normalize_phone(from_number)

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

        # ── Build system prompt immediately (intake questions injected later) ──
        start_timestamp = int(time.time() * 1000)

        # P56 D-06/D-08: fetch MERGED Jobber+Xero caller-context BEFORE
        # build_system_prompt so the STATE+DIRECTIVE block is part of the
        # initial system message. Both providers race CONCURRENTLY within the
        # 2.5s budget; on timeout/error for either, that half silent-skips
        # (Sentry-logged with hashed phone, not raw PII). On BOTH-miss the
        # block is omitted entirely (D-11).
        customer_context = None
        if tenant_id:
            # P56 UAT Test 4 instrumentation: log elapsed + source-provider
            # presence (no PII). Used to verify concurrent-fetch latency
            # stays inside the 2.5s budget when both Jobber + Xero are
            # connected and the system prompt receives both provider
            # markers.
            _ctx_t0 = time.perf_counter()
            customer_context = await fetch_merged_customer_context_bounded(
                tenant_id, from_number, timeout_seconds=2.5
            )
            _ctx_elapsed = time.perf_counter() - _ctx_t0
            _sources = (customer_context or {}).get("_sources") or {}
            _unique_providers = sorted(set(_sources.values()))
            logger.info(
                "[agent] customer_context fetch elapsed=%.3fs providers=%s field_sources=%s",
                _ctx_elapsed,
                _unique_providers or "none",
                _sources or "{}",
            )

        system_prompt = build_system_prompt(
            locale,
            business_name=business_name,
            onboarding_complete=onboarding_complete,
            tone_preset=tone_preset,
            intake_questions="",  # injected after DB query completes
            country=country,
            working_hours=tenant.get("working_hours") if tenant else None,
            tenant_timezone=tenant_timezone,
            customer_context=customer_context,
        )
        local_now = datetime.now(tz=ZoneInfo(tenant_timezone))
        system_prompt += f"\n\nToday is {local_now.strftime('%A, %B %d, %Y')}."

        # Default disconnect reason — tools update this via deps closure
        call_end_reason = ["caller_hangup"]

        # ── Create tools with mutable deps (call_uuid filled in after DB query) ──
        deps = {
            "supabase": supabase,
            "tenant": tenant,
            "tenant_id": tenant_id,
            "call_id": call_id,
            "call_uuid": None,  # updated after call record insert
            "from_number": from_number,
            "to_number": to_number,
            "owner_phone": owner_phone,
            "start_timestamp": start_timestamp,
            "onboarding_complete": onboarding_complete,
            "tenant_timezone": tenant_timezone,
            "room_name": call_id,
            "sip_participant_identity": sip_participant_identity,
            "call_end_reason": call_end_reason,
            "ctx": ctx,
            # Exposed so end_call's _delayed_disconnect can await
            # session.current_speech.wait_for_playout() before tearing down —
            # replaces the legacy 12s fixed sleep that cut off long farewells.
            "session": None,  # populated below after AgentSession(...) is constructed
            # Audit trail of every successful tool execution this call.
            # Tools self-append on completion. Forwarded to post_call for
            # silent hallucination detection (no caller- or owner-facing impact).
            "_tool_call_log": [],
            # P56: merged Jobber+Xero caller-context (pre-fetched above,
            # concurrent per-provider 2.5s budget). None means BOTH providers
            # missed / timed out — check_customer_account tool returns the
            # locked no_customer_match_for_phone string in that case.
            "customer_context": customer_context,
        }
        tools = create_tools(deps)

        # ── Create Gemini model + agent + session ──
        # Use explicitly selected voice if set, else fall back to tone-based mapping (Phase 44: AI Voice Selection)
        ai_voice = tenant.get("ai_voice") if tenant else None
        voice_name = ai_voice if ai_voice else VOICE_MAP.get(tone_preset, "Kore")

        # Dampen Gemini's server-side VAD so it stops cancelling in-flight tool
        # calls on breaths or minor overlap. LOW sensitivity + 1500ms silence
        # threshold (raised from 1000ms in Phase 60.2) tracks upstream guidance
        # for livekit/agents#4441 ("Spurious Server VAD events cause unavoidable
        # tool cancellation") and mitigates the user-visible symptom of
        # livekit/agents#4486 ("Agent audio cuts off after one word"). Barge-in
        # still works — the thresholds just require deliberate caller speech
        # (>1.5s) instead of firing on breath/noise.
        realtime_input_config = genai_types.RealtimeInputConfig(
            automatic_activity_detection=genai_types.AutomaticActivityDetection(
                start_of_speech_sensitivity=genai_types.StartSensitivity.START_SENSITIVITY_LOW,
                end_of_speech_sensitivity=genai_types.EndSensitivity.END_SENSITIVITY_LOW,
                prefix_padding_ms=400,
                silence_duration_ms=1500,  # phase 60.2 (was 1000 in phase 55 999.2 fix)
            ),
        )

        model = google.realtime.RealtimeModel(
            model="gemini-3.1-flash-live-preview",
            voice=voice_name,
            temperature=0.3,
            instructions=system_prompt,
            realtime_input_config=realtime_input_config,
            thinking_config=genai_types.ThinkingConfig(
                thinking_level="minimal",
                include_thoughts=False,
            ),
        )

        agent = VocoAgent(instructions=system_prompt, tools=tools)
        session = AgentSession(llm=model)
        deps["session"] = session

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
        recording_path = f"{tenant_id}/{call_id}.ogg" if tenant_id else f"{call_id}.ogg"

        async def _on_close_async(reason: str = ""):
            end_timestamp = int(time.time() * 1000)
            duration_sec = round((end_timestamp - start_timestamp) / 1000)
            logger.info(
                f"[agent] Session closed: room={call_id} duration={duration_sec}s "
                f"reason={reason or 'unspecified'}"
            )

            # Ensure DB task completed (call_uuid populated) before post-call
            try:
                await db_task
            except Exception:
                pass  # db_task errors already logged inside _run_db_queries

            if egress_id:
                try:
                    lk = api.LiveKitAPI()
                    await lk.egress.stop_egress(api.StopEgressRequest(egress_id=egress_id))
                    await lk.aclose()
                    # Don't poll for S3 upload completion — LiveKit handles the upload
                    # asynchronously on their infrastructure. recording_storage_path was
                    # already written to the calls row at egress start, so the dashboard
                    # finds the file once upload finishes regardless of timing.
                    # Polling here (previously up to 30s) would consume the 10s
                    # shutdown_process_timeout budget and SIGKILL the post-call pipeline.
                except Exception as e:
                    logger.error(f"[agent] Failed to stop egress: {e}")

            try:
                # 8s timeout — safety belt against the SDK's shutdown_process_timeout=10s
                # (worker.py:209). Better to abort cleanly with partial writes than be
                # SIGKILLed mid-write by the parent worker.
                await asyncio.wait_for(
                    run_post_call_pipeline({
                        "supabase": supabase,
                        "call_id": call_id,
                        "call_uuid": deps["call_uuid"],
                        "tenant_id": tenant_id,
                        "tenant": tenant,
                        "from_number": from_number,
                        "to_number": to_number,
                        "start_timestamp": start_timestamp,
                        "end_timestamp": end_timestamp,
                        "transcript_turns": transcript_turns,
                        "recording_storage_path": recording_path if egress_id else None,
                        "is_test_call": is_test_call,
                        "disconnection_reason": call_end_reason[0],
                        # In-memory truth about what booking tools did during the call,
                        # used by post-call to reconcile the DB against mid-call races.
                        "booking_succeeded": deps.get("_booking_succeeded", False),
                        "booked_appointment_id": deps.get("_booked_appointment_id"),
                        "booked_caller_name": deps.get("_booked_caller_name"),
                        # Tool-execution audit trail for silent hallucination detection.
                        "tool_call_log": deps.get("_tool_call_log", []),
                    }),
                    timeout=8.0,
                )
            except asyncio.TimeoutError:
                logger.error(
                    f"[agent] Post-call pipeline TIMEOUT after 8s — partial writes possible. "
                    f"callId={call_id}"
                )
                sentry_sdk.capture_message(
                    f"Post-call pipeline timeout: callId={call_id} tenantId={tenant_id}",
                    level="warning",
                )
            except Exception as e:
                logger.error(f"[agent] Post-call pipeline error: {e}")
                sentry_sdk.capture_exception(e, tags={"callId": call_id, "tenantId": tenant_id, "phase": "post-call"})

        # Register post-call as a JobContext shutdown callback.
        # The SDK awaits all shutdown callbacks inside _run_job_task
        # (job_proc_lazy_main.py:371-379) BEFORE _monitor_task returns and BEFORE
        # loop.shutdown_default_executor() runs (proc_client.py:79). This guarantees
        # asyncio.to_thread() calls inside the post-call pipeline have a live executor.
        #
        # Replaces the previous session.on("close") + asyncio.create_task pattern,
        # which spawned an unowned task that the SDK never awaited — racing the
        # executor teardown and producing "Executor shutdown has been called" errors
        # mid-pipeline (lead creation, owner notifications, hallucination detection
        # were all silently lost).
        ctx.add_shutdown_callback(_on_close_async)

        # ── Launch DB queries as a background task (don't block session start) ──
        # Event signals when session is ready to accept generate_reply() calls
        session_ready = asyncio.Event()

        async def _run_db_queries():
            """Run subscription check, intake questions, and call record insert in parallel."""
            if not tenant_id:
                logger.warning(f"[agent] No tenant for {to_number} — skipping DB queries")
                return

            _db_t0 = time.perf_counter()
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

            # P55: xero caller-context is fetched BEFORE session.start (D-08
            # pre-session injection) and stored on deps["customer_context"]
            # already — no xero task needed inside _run_db_queries.

            results = await asyncio.gather(sub_task, intake_task, call_task, return_exceptions=True)
            logger.info("[agent] _run_db_queries elapsed=%.3fs", time.perf_counter() - _db_t0)

            # Subscription check — disconnect if blocked
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

            # Call record — update deps so tools have the call_uuid (no session dependency)
            if not isinstance(results[2], Exception):
                call_data = results[2].data[0] if results[2].data else None
                if call_data:
                    deps["call_uuid"] = call_data.get("id")
            else:
                logger.error(f"[agent] Call record insert failed: {results[2]}")

            # Intake questions — wait for session to be ready before injecting
            if not isinstance(results[1], Exception) and results[1].data:
                all_questions = []
                for s in results[1].data:
                    for q in (s.get("intake_questions") or []):
                        if q not in all_questions:
                            all_questions.append(q)
                if all_questions:
                    await session_ready.wait()
                    questions_text = "\n".join(all_questions)
                    session.generate_reply(
                        instructions=(
                            f"Additional intake questions to ask naturally during the conversation "
                            f"(skip any already answered):\n{questions_text}"
                        ),
                    )

        # Fire DB queries in background — they complete while session starts + greeting plays
        db_task = asyncio.create_task(_run_db_queries())

        # ── Start session (awaited — Gemini WebSocket handshake) ──
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

        # ── Generate greeting immediately after session starts ──
        session.generate_reply(
            instructions="Greet the caller now.",
        )
        session_ready.set()  # signal DB task that session is ready for generate_reply()

        # ── Start Egress recording (non-blocking) ──
        async def _start_egress():
            nonlocal egress_id
            # Wait for DB task so call_uuid is available for egress tracking
            await db_task
            try:
                lk = api.LiveKitAPI()
                egress_info = await lk.egress.start_room_composite_egress(
                    api.RoomCompositeEgressRequest(
                        room_name=call_id,
                        audio_only=True,
                        file_outputs=[api.EncodedFileOutput(
                            file_type=api.EncodedFileType.OGG,
                            filepath=recording_path,
                            disable_manifest=True,
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

                if deps.get("call_uuid"):
                    await asyncio.to_thread(
                        lambda: supabase.table("calls").update({
                            "egress_id": egress_id,
                            "recording_storage_path": recording_path,
                        }).eq("call_id", call_id).execute()
                    )
            except Exception as e:
                logger.error(f"[agent] Failed to start egress: {e}")

        asyncio.create_task(_start_egress())

    except Exception as e:
        logger.error(f"[agent] Entry function error: {e}", exc_info=True)
        sentry_sdk.capture_exception(e)
        raise

    # Entrypoint returns here. The SDK does NOT await the entrypoint task to
    # decide when the job is done — it awaits _shutdown_fut (which resolves on
    # room disconnect or ctx.shutdown()). Post-call work is registered above as
    # a JobContext shutdown_callback, which the SDK awaits inside _run_job_task
    # before tearing down the asyncio default executor. This is the canonical
    # LiveKit Agents 1.5 pattern for post-call cleanup with DB I/O.


if __name__ == "__main__":
    start_webhook_server()
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="voco-voice-agent",
        )
    )
