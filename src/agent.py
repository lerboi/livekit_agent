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
import re
import json
import time
import hashlib
import asyncio
import logging
from datetime import datetime, timezone
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
from livekit.plugins.google.beta.gemini_tts import TTS as GeminiTTS
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

# Phase 60.4 Stream B (D-B-01): Gemini Live input-language hint.
# Native-audio model (gemini-3.1-flash-live-preview) may auto-detect
# regardless, but the kwarg is defense-in-depth; paired with an
# anti-hallucination prompt directive per _build_language_section.
# Map restricted to locales with prompt-side ES branches (60.3 Plan 12);
# unknown locales fall back to en-US.
_LOCALE_TO_BCP47 = {
    "en": "en-US",
    "es": "es-US",
}


def _locale_to_bcp47(locale: str | None) -> str:
    if not locale:
        return "en-US"
    return _LOCALE_TO_BCP47.get(locale, "en-US")

# Timeout for SIP participant to join the room (seconds)
PARTICIPANT_TIMEOUT_S = 30


# ── Phase 60.3 Stream A: goodbye-race diagnostic instrumentation ──────────

class _GoodbyeDiagHandler(logging.Handler):
    """Captures text_done/audio_done from the livekit.agents
    _SegmentSynchronizerImpl.playback_finished warning (R-A3).

    Attached to logging.getLogger("livekit.agents") per-call; removed in
    _flush_goodbye_diag() to prevent per-call handler accumulation.

    The warning is emitted at synchronizer.py:268-279 with extra=
    {"text_done": bool, "audio_done": bool}, which standard logging
    semantics surface as attributes on the LogRecord.
    """
    def __init__(self, diag_record):
        super().__init__()
        self._diag_record = diag_record

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if "playback_finished called before text/audio" in record.getMessage():
                self._diag_record[0]["playback_finished_at"] = int(time.time() * 1000)
                self._diag_record[0]["text_done"] = getattr(record, "text_done", None)
                self._diag_record[0]["audio_done"] = getattr(record, "audio_done", None)
        except Exception:
            pass  # diagnostic handler must never raise


# Redaction regex for E.164-shaped phone numbers in transcript tails.
# Matches an optional '+' followed by 7-15 digits (E.164 standard length).
# Applied to transcript_tail so caller-spoken or otherwise-captured numbers
# never land in Railway logs / Sentry breadcrumbs (T-60.3-01 mitigation).
_PHONE_REDACT_RE = re.compile(r"\+?\d{7,15}")


async def _flush_goodbye_diag(
    *,
    diag_record: list,
    transcript_turns: list,
    tool_call_log: list,
    goodbye_handler: logging.Handler,
) -> None:
    """Phase 60.3 Stream A (R-A7): flush the goodbye-race diagnostic record
    as a [goodbye_race] logger.info line + Sentry breadcrumb, then remove
    the _GoodbyeDiagHandler from the livekit.agents logger.

    This helper is invoked as the FIRST statement of _on_close_async so the
    record survives the post-call pipeline's 8s timeout (Fix I monitoring).

    transcript_tail is phone-redacted (T-60.3-01): any E.164-shaped substring
    is replaced with '[PHONE]' before serialization — protects both the
    SIP-attrs-known from_number AND anything the caller spoke aloud.
    """
    try:
        rec = diag_record[0]
        tail_parts = []
        for turn in transcript_turns[-3:]:
            tail_parts.append(f"{turn.get('role', '?')}: {turn.get('content', '')}")
        transcript_tail = " | ".join(tail_parts)[:500]
        rec["transcript_tail"] = _PHONE_REDACT_RE.sub("[PHONE]", transcript_tail)
        rec["tool_call_log_tail"] = list((tool_call_log or []))[-5:]
        logger.info("[goodbye_race] %s", json.dumps(rec, default=str))
        sentry_sdk.add_breadcrumb(
            category="goodbye_race",
            message="Call ended — diag record",
            data=rec,
            level=("warning" if rec.get("text_done") is False else "info"),
        )
    except Exception as e:
        logger.warning("[goodbye_race] flush failed: %s", e)
    finally:
        try:
            logging.getLogger("livekit.agents").removeHandler(goodbye_handler)
        except Exception:
            pass


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

        # Phase 63.1: hoist intake_questions fetch BEFORE session.start().
        # Replaces the broken post-session session.generate_reply(...) injection
        # that silently fails on livekit-plugins-google 1.5.6 + gemini-3.1-flash-live-preview
        # (capability guard mutable_chat_context=False blocks generate_reply entirely).
        # Per D-02: extra ~100-200ms pre-start latency is acceptable — correctness wins.
        intake_questions_text = ""
        if tenant_id:
            try:
                _intake_res = await asyncio.to_thread(
                    lambda: supabase.table("services")
                        .select("intake_questions")
                        .eq("tenant_id", tenant_id)
                        .eq("is_active", True)
                        .execute()
                )
                all_q: list[str] = []
                for s in (_intake_res.data or []):
                    for q in (s.get("intake_questions") or []):
                        if q and q not in all_q:
                            all_q.append(q)
                intake_questions_text = "\n".join(all_q)
                logger.info("[63.1] intake_questions injected count=%d", len(all_q))
            except Exception as e:
                logger.warning("[63.1] intake_questions fetch failed, continuing with empty: %s", e)

        system_prompt = build_system_prompt(
            locale,
            business_name=business_name,
            onboarding_complete=onboarding_complete,
            tone_preset=tone_preset,
            intake_questions=intake_questions_text,
            country=country,
            working_hours=tenant.get("working_hours") if tenant else None,
            tenant_timezone=tenant_timezone,
            customer_context=customer_context,
        )
        local_now = datetime.now(tz=ZoneInfo(tenant_timezone))
        system_prompt += f"\n\nToday is {local_now.strftime('%A, %B %d, %Y')}."

        # Default disconnect reason — tools update this via deps closure
        call_end_reason = ["caller_hangup"]

        # Phase 60.3 Stream A: per-call goodbye-race diagnostic record (R-A6).
        # Single-element-list closure pattern mirrors call_end_reason above.
        # The SAME list is referenced via `deps["_diag_record"]` (for tool
        # writes) AND via the entrypoint closure `diag_record` (for
        # session-level handler writes).
        diag_record = [{
            "schema_version": 1,
            "call_id": call_id,
            "tenant_id": tenant_id,
            "caller_phone_sha256": (
                hashlib.sha256(from_number.encode("utf-8")).hexdigest()[:16]
                if from_number else None
            ),
            "started_at_ms": int(time.time() * 1000),
        }]

        # Install the _GoodbyeDiagHandler on the livekit.agents logger so the
        # _SegmentSynchronizerImpl.playback_finished warning's text_done /
        # audio_done extra= fields land on diag_record[0] (R-A3). The handler
        # is removed in _flush_goodbye_diag() at call close.
        _goodbye_handler = _GoodbyeDiagHandler(diag_record)
        logging.getLogger("livekit.agents").addHandler(_goodbye_handler)

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
            # Phase 60.3 Stream A: per-call goodbye-race diagnostic record.
            # Same list reference as the entrypoint closure `diag_record`
            # above — tools write via deps, session-level handlers write via
            # the closure. end_call.py writes end_call_invoked_at here.
            "_diag_record": diag_record,
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
        logger.info(
            "[agent] voice_resolved tenant_ai_voice=%r tone_preset=%r -> voice=%r",
            ai_voice, tone_preset, voice_name,
        )

        # Dampen Gemini's server-side VAD so it stops cancelling in-flight tool
        # calls on breaths or minor overlap. LOW sensitivity + 1500ms silence
        # threshold (raised from 1000ms in Phase 60.2) tracks upstream guidance
        # for livekit/agents#4441. Barge-in still works — thresholds just
        # require deliberate caller speech (>1.5s) instead of firing on
        # breath/noise.
        #
        # Phase 63.1-10 revert: activity_handling=NO_INTERRUPTION was tried in
        # 63.1-08 to stop tool-call cancellation, but it caused a worse
        # failure mode — after check_availability returned, Gemini never
        # verbalized the result and the call stalled indefinitely. Reverting
        # to default (START_OF_ACTIVITY_INTERRUPTS) and addressing the
        # tool-retry-loop at the prompt level (see _build_booking_section
        # anti-retry guidance).
        realtime_input_config = genai_types.RealtimeInputConfig(
            automatic_activity_detection=genai_types.AutomaticActivityDetection(
                start_of_speech_sensitivity=genai_types.StartSensitivity.START_SENSITIVITY_LOW,
                end_of_speech_sensitivity=genai_types.EndSensitivity.END_SENSITIVITY_LOW,
                prefix_padding_ms=400,
                # Phase 63.1-11: raised 1500 -> 2500ms. Live UAT shows the
                # caller saying brief acknowledgments ("hello", "mhm")
                # during agent speech fires VAD at 1500ms, triggering
                # `server cancelled tool calls` + mid-word truncation
                # (#4486 pipeline race). 2500ms requires a deliberate
                # full-sentence utterance (>2.5s of continuous speech)
                # before VAD fires — brief acknowledgments no longer
                # count as interrupts. Barge-in still works for genuine
                # caller interjections.
                silence_duration_ms=2500,
            ),
        )

        logger.info(
            "[60.4 Stream B] RealtimeModel language=%s (locale=%s)",
            _locale_to_bcp47(locale),
            locale,
        )

        # Phase-fix (2026-04-24): Gemini 3 explicit guidance says temperature
        # must be left at default (1.0); custom values "risk looping or
        # degraded performance." We were overriding to 0.3 (carryover from
        # Gemini 2.x tool-agent convention) and observing exactly the
        # symptom Google warns about: filler loops + hallucinated outcomes.
        # Deleted.
        #
        # thinking_level raised from "minimal" to "low": minimal mode
        # short-circuits tool-vs-speak deliberation, so the model pattern-
        # matches to the most fluent continuation (e.g. fabricating a
        # booking confirmation) instead of invoking a tool. "low" keeps
        # latency tight while restoring enough deliberation to pick tools
        # over fluent fabrication on a receptionist call.
        model = google.realtime.RealtimeModel(
            model="gemini-3.1-flash-live-preview",
            voice=voice_name,
            language=_locale_to_bcp47(locale),  # Phase 60.4 D-B-01: best-effort STT pin on native-audio
            instructions=system_prompt,
            realtime_input_config=realtime_input_config,
            thinking_config=genai_types.ThinkingConfig(
                thinking_level="low",
                include_thoughts=False,
            ),
        )

        agent = VocoAgent(instructions=system_prompt, tools=tools)

        # Phase 63.1-06 (gap-closure, v2): Gemini TTS plugin attached so
        # session.say() can synthesize the opening greeting. The Gemini 3.1
        # Live RealtimeModel does NOT support agent-first turns via any of
        # session.generate_reply / session.say-via-realtime /
        # update_chat_ctx (all capability-gated closed; see
        # realtime_api.py:289 `mutable = "3.1" not in model`). The only
        # confirmed path on the current SDK (livekit-agents==1.5.6 +
        # livekit-plugins-google==1.5.6) is to attach a separate TTS
        # pipeline and call session.say() — agent_activity.py:1041-1095
        # routes say() through _tts_task whenever self.tts is set. The
        # Gemini TTS `voice_name` set matches the Gemini Live voice set 1:1
        # (Zephyr, Puck, Kore, etc.) so the greeting voice is IDENTICAL to
        # the subsequent conversation voice — no audible switch.
        greeting_tts = GeminiTTS(
            voice_name=voice_name,
            model="gemini-2.5-flash-preview-tts",
            # Gemini TTS controls pace via natural-language instructions rather
            # than a numeric speaking_rate. Ask for a noticeably brisk delivery
            # so the ~114-char greeting finishes in ~4-5s instead of 7-8s —
            # keeps the pre-greeting protected window short and gets the
            # caller to the actionable question faster.
            # Direct pace instruction — Gemini TTS prepends the full string
            # to the text as context (gemini_tts.py:200-201), so keep it
            # short and declarative. "Quickly" is more reliably honored than
            # abstract adjectives like "brisk" or "efficient".
            instructions="Say this quickly, in a warm professional tone:",
        )
        session = AgentSession(llm=model, tts=greeting_tts)
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
                # Phase 60.3 Stream A (R-A1): capture last_text_token_at on
                # agent turns only — the last agent turn before end_call IS
                # the goodbye.
                if role == "agent":
                    diag_record[0]["last_text_token_at"] = int(event.created_at * 1000)

        # Phase 60.3 Stream A (R-A5): session-level close event →
        # session_close_at + close_reason enum string.
        @session.on("close")
        def _on_close_event(event):
            try:
                diag_record[0]["session_close_at"] = int(event.created_at * 1000)
                diag_record[0]["close_reason"] = event.reason.value
            except Exception:
                pass

        # Phase 60.3 Stream A (R-A5): room-level participant_disconnected →
        # disconnect_reason (CLIENT_INITIATED vs SERVER_INITIATED) which
        # CloseEvent does not expose. Only captures the SIP caller; agent-side
        # disconnects route through session.on("close").
        @ctx.room.on("participant_disconnected")
        def _on_participant_disconnected(participant):
            try:
                if participant.identity == sip_participant_identity:
                    diag_record[0]["participant_disconnect_at"] = int(time.time() * 1000)
                    dr = participant.disconnect_reason or rtc.DisconnectReason.UNKNOWN_REASON
                    diag_record[0]["disconnect_reason"] = rtc.DisconnectReason.Name(dr)
            except Exception:
                pass

        # ── Session error handler ──
        @session.on("error")
        def on_error(event):
            logger.error(f"[agent] Session error: room={call_id} tenant={tenant_id} error={event.error}")
            actual_error = getattr(event.error, "error", event.error)
            sentry_sdk.capture_exception(actual_error)

        # [63.1-DIAG] Session state diagnostics — instrument every relevant
        # event so a stall after a tool call can be traced through Gemini's
        # state machine. Log lines prefix [63.1-DIAG] for easy grep.
        @session.on("agent_state_changed")
        def _diag_agent_state(event):
            try:
                logger.info(
                    "[63.1-DIAG] agent_state %s -> %s at=%.3f",
                    event.old_state, event.new_state, event.created_at,
                )
            except Exception:
                pass

        @session.on("user_state_changed")
        def _diag_user_state(event):
            try:
                logger.info(
                    "[63.1-DIAG] user_state %s -> %s at=%.3f",
                    event.old_state, event.new_state, event.created_at,
                )
            except Exception:
                pass

        @session.on("function_tools_executed")
        def _diag_tools_executed(event):
            try:
                summary = []
                for fc, out in event.zipped():
                    name = getattr(fc, "name", "?")
                    args = getattr(fc, "arguments", None)
                    args_preview = str(args)[:100] if args else ""
                    out_text = getattr(out, "output", None) if out else None
                    out_preview = (out_text or "")[:180].replace("\n", " \\n ")
                    out_len = len(out_text or "") if out_text else 0
                    summary.append(
                        f"{name}(args={args_preview!r}) -> len={out_len} preview={out_preview!r}"
                    )
                logger.info(
                    "[63.1-DIAG] function_tools_executed count=%d %s",
                    len(summary), " | ".join(summary),
                )
            except Exception as e:
                logger.warning(f"[63.1-DIAG] function_tools_executed log failed: {e}")

        @session.on("speech_created")
        def _diag_speech_created(event):
            try:
                logger.info(
                    "[63.1-DIAG] speech_created user_initiated=%s source=%s",
                    getattr(event, "user_initiated", None),
                    getattr(event, "source", None),
                )
            except Exception:
                pass

        @session.on("agent_false_interruption")
        def _diag_false_interruption(event):
            logger.warning(
                "[63.1-DIAG] agent_false_interruption — unexpected silence / dropped generation"
            )

        # ── Handle session end (post-call pipeline) — registered BEFORE start to avoid race ──
        egress_id = None
        recording_path = f"{tenant_id}/{call_id}.ogg" if tenant_id else f"{call_id}.ogg"

        async def _on_close_async(reason: str = ""):
            # Phase 60.3 Stream A (R-A7): flush goodbye-race diagnostic record
            # FIRST. If run_post_call_pipeline below times out (8s Fix I
            # monitoring), the record is already logged to Railway and
            # attached as a Sentry breadcrumb. Handler cleanup happens inside
            # the helper's finally block so it runs even on flush error.
            await _flush_goodbye_diag(
                diag_record=diag_record,
                transcript_turns=transcript_turns,
                tool_call_log=deps.get("_tool_call_log", []) or [],
                goodbye_handler=_goodbye_handler,
            )

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

        async def _run_db_queries():
            """Run subscription check and call record insert in parallel with slot cache prefetch."""
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

            # Phase-fix (2026-04-23): Prefetch scheduling data at session init.
            # Gemini Live cancels in-flight function calls if the caller speaks
            # while a tool is pending; shortening check_availability from ~500ms
            # (5 live Supabase queries) to ~50ms (pure slot math over cached
            # data) narrows that race. Cache is consumed by check_availability
            # with a 30s TTL; stale reads fall through to the live-fetch path.
            _now_iso = datetime.now(timezone.utc).isoformat()
            slot_appts_task = asyncio.to_thread(
                lambda: supabase.table("appointments")
                .select("start_time, end_time, zone_id")
                .eq("tenant_id", tenant_id)
                .neq("status", "cancelled")
                .neq("status", "completed")
                .gte("end_time", _now_iso)
                .execute()
            )
            slot_events_task = asyncio.to_thread(
                lambda: supabase.table("calendar_events")
                .select("start_time, end_time")
                .eq("tenant_id", tenant_id)
                .gte("end_time", _now_iso)
                .execute()
            )
            slot_zones_task = asyncio.to_thread(
                lambda: supabase.table("service_zones")
                .select("id, name, postal_codes")
                .eq("tenant_id", tenant_id)
                .execute()
            )
            slot_buffers_task = asyncio.to_thread(
                lambda: supabase.table("zone_travel_buffers")
                .select("zone_a_id, zone_b_id, buffer_mins")
                .eq("tenant_id", tenant_id)
                .execute()
            )
            slot_blocks_task = asyncio.to_thread(
                lambda: supabase.table("calendar_blocks")
                .select("start_time, end_time")
                .eq("tenant_id", tenant_id)
                .gte("end_time", _now_iso)
                .execute()
            )

            # P55: xero caller-context is fetched BEFORE session.start (D-08
            # pre-session injection) and stored on deps["customer_context"]
            # already — no xero task needed inside _run_db_queries.

            # Phase 63.1: intake_questions hoisted pre-session (see above).
            # Remaining parallel tasks unpacked by name per Pitfall 2.
            sub_res, call_res, appts_res, events_res, zones_res, buffers_res, blocks_res = await asyncio.gather(
                sub_task, call_task,
                slot_appts_task, slot_events_task, slot_zones_task, slot_buffers_task, slot_blocks_task,
                return_exceptions=True,
            )
            logger.info("[agent] _run_db_queries elapsed=%.3fs", time.perf_counter() - _db_t0)

            # Stash prefetched scheduling data on deps for check_availability.
            def _safe_data(r):
                return (r.data if not isinstance(r, Exception) and r and r.data else []) or []
            deps["_slot_cache"] = {
                "fetched_at": time.time(),
                "appointments": _safe_data(appts_res),
                "calendar_events": _safe_data(events_res),
                "service_zones": _safe_data(zones_res),
                "zone_travel_buffers": _safe_data(buffers_res),
                "calendar_blocks": _safe_data(blocks_res),
            }
            logger.info(
                "[agent] _slot_cache prefetched appts=%d events=%d zones=%d buffers=%d blocks=%d",
                len(deps["_slot_cache"]["appointments"]),
                len(deps["_slot_cache"]["calendar_events"]),
                len(deps["_slot_cache"]["service_zones"]),
                len(deps["_slot_cache"]["zone_travel_buffers"]),
                len(deps["_slot_cache"]["calendar_blocks"]),
            )

            # Subscription check — disconnect if blocked
            if not isinstance(sub_res, Exception):
                sub_data = sub_res.data
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
                logger.warning(f"[agent] Subscription check failed (allowing call): {sub_res}")

            # Call record — update deps so tools have the call_uuid (no session dependency)
            if not isinstance(call_res, Exception):
                call_data = call_res.data[0] if call_res.data else None
                if call_data:
                    deps["call_uuid"] = call_data.get("id")
            else:
                logger.error(f"[agent] Call record insert failed: {call_res}")

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

        # Phase 60.3 Stream A (R-A2): wrap session.output.audio.capture_frame
        # to stamp last_audio_frame_at on every emitted frame. Per Pitfall 2
        # this wrap MUST happen AFTER session.start() — audio is None before.
        # Null-guarded because some session configurations may lack audio.
        try:
            if session.output.audio is not None:
                _original_capture_frame = session.output.audio.capture_frame

                async def _timed_capture_frame(frame):
                    diag_record[0]["last_audio_frame_at"] = int(time.time() * 1000)
                    return await _original_capture_frame(frame)

                session.output.audio.capture_frame = _timed_capture_frame  # type: ignore[method-assign]
        except Exception as e:
            logger.warning(f"[agent] Failed to install goodbye-race audio frame wrapper: {e}")

        # Phase 63.1-06 (gap-closure, v2): speak the opening greeting via the
        # attached Gemini TTS pipeline. Fire-and-forget — the SpeechHandle
        # returned by session.say() schedules the synthesis task; the caller
        # hears the greeting within ~500ms of SIP audio coming up. The TTS
        # voice matches Gemini Live's voice_name so the subsequent
        # conversation is audibly identical. The greeting text is templated
        # per-tenant (business_name + localized recording disclosure); this
        # MUST match the prompt's OPENING guidance so Gemini's follow-up
        # responses feel contextually consistent.
        if locale == "es":
            disclosure_text = "Esta llamada puede ser grabada por motivos de calidad."
            if onboarding_complete:
                greeting_text = (
                    f"Hola, gracias por llamar a {business_name}. "
                    f"{disclosure_text} ¿En qué puedo ayudarle?"
                )
            else:
                greeting_text = (
                    f"{disclosure_text} ¿En qué puedo ayudarle?"
                )
        else:
            disclosure_text = "This call may be recorded for quality purposes."
            if onboarding_complete:
                greeting_text = (
                    f"Hello, thank you for calling {business_name}. "
                    f"{disclosure_text} How can I help you today?"
                )
            else:
                greeting_text = (
                    f"{disclosure_text} How can I help you today?"
                )
        # Phase 63.1-07: mute Gemini's input audio for the duration of the
        # TTS greeting. Without this, one of two things happens:
        #   (a) SIP acoustic echo feeds TTS audio back as user audio →
        #       Gemini's server VAD fires → Gemini interrupts its own
        #       greeting or produces a confused response.
        #   (b) The caller speaks over the greeting (intentional or
        #       accidental) and barge-in fires prematurely before they
        #       hear the question they're supposed to answer.
        # Disabling input audio during greeting playback, then re-enabling
        # once playout completes, gives a clean ~3-5s protected window.
        # Hard 6s safety cap ensures we never leave input permanently
        # muted if playout signaling hangs.
        try:
            session.input.set_audio_enabled(False)
            greeting_handle = session.say(greeting_text)
            logger.info(
                "[63.1-06] greeting dispatched via TTS locale=%s chars=%d voice=%s (input muted)",
                locale, len(greeting_text), voice_name,
            )

            async def _unmute_after_greeting():
                try:
                    # Raised from 6s → 10s after live UAT showed Gemini TTS
                    # synthesis of the 114-char branded greeting exceeding 6s
                    # end-to-end (including first-audio-frame latency), causing
                    # the force-unmute to fire while the greeting was still
                    # playing. 10s is comfortable headroom for any normal
                    # greeting length; the safety cap still prevents permanent
                    # mute on broken playout signaling.
                    await asyncio.wait_for(greeting_handle.wait_for_playout(), timeout=10.0)
                    logger.info("[63.1-07] greeting playout complete; unmuting input")
                except asyncio.TimeoutError:
                    logger.warning(
                        "[63.1-07] greeting playout wait timed out at 10s; force-unmuting input"
                    )
                except Exception as e:
                    logger.error(f"[63.1-07] greeting playout wait failed: {e}; force-unmuting")
                finally:
                    try:
                        session.input.set_audio_enabled(True)
                    except Exception as e2:
                        logger.error(f"[63.1-07] failed to re-enable input audio: {e2}")

            asyncio.create_task(_unmute_after_greeting())
        except Exception as e:
            logger.error(f"[63.1-06] session.say() failed: {e}")
            # Failed to dispatch greeting — make sure input is enabled so the
            # caller can still talk to Gemini.
            try:
                session.input.set_audio_enabled(True)
            except Exception:
                pass

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
