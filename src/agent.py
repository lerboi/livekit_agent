"""
Voco LiveKit Voice Agent

Main entry point for the AI receptionist agent.
  Twilio SIP -> LiveKit -> cascaded pipeline:
    Deepgram STT (nova-3 multi) -> OpenAI gpt-4.1-mini LLM -> ElevenLabs Flash TTS

Architecture:
- Each inbound call creates a LiveKit room via SIP dispatch rule
- This agent joins the room, looks up the tenant, and opens a cascaded
  AgentSession (stt + llm + tts + vad + turn detection)
- All 9 tools execute in-process (no webhook round-trips)
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

from livekit.agents import AgentSession, Agent, cli, JobContext, WorkerOptions, room_io
from livekit.plugins import openai, deepgram, elevenlabs, silero, noise_cancellation
from livekit.plugins.turn_detector.multilingual import MultilingualModel
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


# Phase 66: locale message bundles for the deterministic session.say() greeting.
# Loaded once at import (mirrors prompt.py's loader; kept local here to avoid
# importing prompt.py's private _messages). _msg() resolves a dotted key.
_MESSAGES_DIR = os.path.join(os.path.dirname(__file__), "messages")
with open(os.path.join(_MESSAGES_DIR, "en.json"), "r", encoding="utf-8") as _f:
    _EN_MESSAGES = json.load(_f)
with open(os.path.join(_MESSAGES_DIR, "es.json"), "r", encoding="utf-8") as _f:
    _ES_MESSAGES = json.load(_f)
_MESSAGE_BUNDLES = {"en": _EN_MESSAGES, "es": _ES_MESSAGES}


def _msg(locale: str, key: str) -> str:
    """Resolve a dotted message key (e.g. 'agent.greeting_default') for a locale,
    falling back to English then to the key itself."""
    parts = key.split(".")
    val = _MESSAGE_BUNDLES.get(locale) or _MESSAGE_BUNDLES["en"]
    for part in parts:
        if isinstance(val, dict):
            val = val.get(part)
        else:
            return key
    return val if val is not None else key

# Hard cap (seconds) for waiting on the opening greeting to finish playing before
# we force-unmute caller input. The greeting runs ~3-5s; this only fires if a SIP
# playout stalls or drops, guaranteeing input is never left muted.
GREETING_UNMUTE_TIMEOUT_S = 10.0

# Phase 66: LLM (the decision-maker / tool-caller) for the cascaded pipeline.
# gpt-4.1-mini is non-reasoning -> low time-to-first-token, strong tool calling,
# cheap, and uses the already-installed livekit-plugins-openai (openai.LLM).
# Isolated as a single constant so a model change is one edit. The plugin accepts
# any string; a wrong id fails at the first live call, not at import — confirm at
# the UAT gate.
LLM_MODEL = "gpt-4.1-mini"

# Phase 66: ElevenLabs TTS model. Flash v2.5 streams first-byte ~75ms — the
# sub-500ms TTS the Phase-64 revert doc named as the prerequisite for a viable
# cascaded pipeline (GeminiTTS's ~1.3s first-byte is what killed Phase 64).
ELEVENLABS_TTS_MODEL = "eleven_flash_v2_5"

# Voice mapping: tone_preset/ai_voice LABEL -> ElevenLabs voice_id.
# The DB stores a stable LABEL (professional / friendly / local_expert) in
# tenants.ai_voice (main repo migration 068), NOT a raw voice_id — so swapping an
# ElevenLabs voice is a one-line change here and never needs a DB migration.
# professional is the standard/default and the fallback when tone_preset/ai_voice
# is unknown or NULL.
#
# Each voice_id MUST be added to the ElevenLabs account's "My Voices" (livekit/
# agents #3992 — the plugin cannot use a voice that is not in My Voices and
# hard-fails the call otherwise). Only two voices were provided; local_expert
# reuses the professional voice (its prompt persona still differs via the
# tone_preset TONE_LABELS). Kept in sync with main-repo migration 068 +
# src/lib/ai-voice-validation.js (which store/validate the LABELS, not the ids).
ELEVENLABS_VOICE_MAP = {
    "professional": "BIvP0GN1cAtSRTxNHnWS",
    "friendly": "7EzWGsX10sAS4c9m9cPf",
    "local_expert": "BIvP0GN1cAtSRTxNHnWS",  # no separate voice provided -> reuse professional
}

# Valid stored-label allowlist (the keys of the voice map).
ELEVENLABS_VOICE_LABELS = frozenset(ELEVENLABS_VOICE_MAP.keys())


def _resolve_voice(ai_voice, tone_preset):
    """Resolve the ElevenLabs voice_id for a call.

    tenants.ai_voice stores a stable LABEL (professional / friendly /
    local_expert) or NULL. Use the tenant's explicitly selected label when it is
    a known label; otherwise fall back to the tone_preset label; otherwise the
    professional default. Returns an ElevenLabs voice_id string.

    Until the dashboard picker (main repo) is updated and tenants re-select,
    ai_voice is NULL (migration 068 clears it) -> tone fallback, which is safe.
    A stale OpenAI-era value (e.g. "marin") is not a known label -> also falls
    back cleanly.
    """
    if ai_voice in ELEVENLABS_VOICE_LABELS:
        return ELEVENLABS_VOICE_MAP[ai_voice]
    if tone_preset in ELEVENLABS_VOICE_MAP:
        return ELEVENLABS_VOICE_MAP[tone_preset]
    return ELEVENLABS_VOICE_MAP["professional"]


# Subscription statuses that block inbound calls
BLOCKED_STATUSES = ["canceled", "paused", "incomplete"]

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
        #
        # Phase 62: caller_history (Voco's own customers/jobs/inquiries/
        # appointments tables) is fetched IN PARALLEL with customer_context.
        # Pre-session injection eliminates the 3-5s first-turn silent gap
        # caused by the prior eager-invoke check_caller_history pattern
        # (call AJ_bFP3MLdqnKqT, 2026-05-07). Both fetches share the same
        # 2.5s budget — completion happens during greeting playout (~5-7s)
        # so caller-perceived latency is zero.
        customer_context = None
        caller_history = None
        if tenant_id:
            from .tools.check_caller_history import fetch_caller_history

            async def _fetch_caller_history_bounded():
                if not from_number:
                    return None
                try:
                    return await asyncio.wait_for(
                        fetch_caller_history(
                            supabase, tenant_id, from_number, tenant_timezone
                        ),
                        timeout=2.5,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[agent] caller_history fetch timeout — proceeding without"
                    )
                    return None
                except Exception as e:
                    logger.warning(
                        "[agent] caller_history fetch failed: %s — proceeding without", e
                    )
                    return None

            _ctx_t0 = time.perf_counter()
            customer_context, caller_history = await asyncio.gather(
                fetch_merged_customer_context_bounded(
                    tenant_id, from_number, timeout_seconds=2.5
                ),
                _fetch_caller_history_bounded(),
            )
            _ctx_elapsed = time.perf_counter() - _ctx_t0
            _sources = (customer_context or {}).get("_sources") or {}
            _unique_providers = sorted(set(_sources.values()))
            _history_state = (
                "repeat_caller" if caller_history else
                ("first_time_caller" if caller_history == {} else "none")
            )
            logger.info(
                "[agent] customer_context+caller_history fetch elapsed=%.3fs "
                "providers=%s field_sources=%s history=%s",
                _ctx_elapsed,
                _unique_providers or "none",
                _sources or "{}",
                _history_state,
            )

        # Hoist the intake_questions fetch BEFORE session.start() so the questions
        # are part of the initial system prompt (built below) rather than injected
        # mid-session. Extra ~100-200ms pre-start latency is acceptable —
        # correctness wins.
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
            caller_history=caller_history,
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
            # Tenant's ISO country code (e.g. "GB", "AU"). Read by
            # book_appointment.py / capture_lead.py as the address-validation
            # region_code; without it those tools always fell back to "US"
            # for non-US tenants (region defaulted wrong on every non-US call).
            "country": country,
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
            # Phase 62: pre-fetched caller history from Voco's own
            # customers/jobs/inquiries/appointments tables. Same 2.5s budget,
            # parallel with customer_context. None means fetch failed/empty.
            # Already injected into the system prompt via
            # _build_caller_history_section — exposed here for any future
            # tool/handler that needs the structured dict.
            "caller_history": caller_history,
        }
        tools = create_tools(deps)

        # ── Resolve the ElevenLabs voice + build the cascaded pipeline session ──
        # tenants.ai_voice stores a stable LABEL (professional/friendly/local_expert)
        # or NULL; _resolve_voice maps it (or the tone_preset) to an ElevenLabs
        # voice_id. A stale OpenAI-era value or NULL falls back to the tone voice,
        # so the agent never depends on migration 068 having cleared ai_voice.
        ai_voice = tenant.get("ai_voice") if tenant else None
        voice_id = _resolve_voice(ai_voice, tone_preset)
        if ai_voice and ai_voice not in ELEVENLABS_VOICE_LABELS:
            logger.warning(
                "[agent] unrecognized ai_voice=%r (not an ElevenLabs voice label) "
                "— using tone default voice_id=%r", ai_voice, voice_id,
            )
        logger.info(
            "[agent] voice_resolved tenant_ai_voice=%r tone_preset=%r -> voice_id=%r",
            ai_voice, tone_preset, voice_id,
        )

        # Phase 66: cascaded STT -> LLM -> TTS pipeline (replaces the single
        # gpt-realtime-2 speech-to-speech model). The migration rationale is
        # tool-calling reliability: a strong text LLM (gpt-4.1-mini) is a more
        # reliable, debuggable tool-caller than a realtime speech model, running
        # on LiveKit's mature pipeline plugin APIs.
        #
        # STT (D1 default): Deepgram nova-3 with language="multi" preserves EN+ES
        # code-switching. Deliberately isolated to these two lines so the STT is
        # one-line-swappable — AssemblyAI Universal-3 Pro and Deepgram Flux-multi
        # are the UAT A/B candidates for alphanumeric/address accuracy (see
        # My Prompts/Migration.md §D1). MultilingualModel() supplies semantic
        # end-of-turn detection (more robust to brief SIP echo than raw Silero
        # endpointing) and needs the model files the Dockerfile pre-downloads.
        stt = deepgram.STT(model="nova-3", language="multi")
        turn_detection = MultilingualModel()

        # LLM: non-reasoning gpt-4.1-mini for low TTFT + strong tool calling.
        # parallel_tool_calls=False keeps the booking flow strictly sequential
        # (e.g. never fires check_slot and book_appointment in one turn) — the
        # slot_token contract assumes one tool call resolves before the next.
        llm = openai.LLM(model=LLM_MODEL, parallel_tool_calls=False)

        # TTS: ElevenLabs Flash v2.5 (~75ms first-byte) — the sub-500ms TTS that
        # makes this pipeline viable where Phase 64's GeminiTTS (~1.3s) did not.
        tts = elevenlabs.TTS(model=ELEVENLABS_TTS_MODEL, voice_id=voice_id)

        # VAD: Silero defaults for barge-in. DO NOT port the realtime model's
        # 2.5s silence value here — Phase 64 did exactly that and added ~2s/turn.
        vad = silero.VAD.load()

        agent = VocoAgent(instructions=system_prompt, tools=tools)

        session = AgentSession(
            stt=stt,
            llm=llm,
            tts=tts,
            vad=vad,
            turn_detection=turn_detection,
            # Callers must be able to barge in (emergencies). Echo defense for the
            # OPENING line is the input-mute below, not disabling interruptions.
            allow_interruptions=True,
        )
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
        # event so a stall after a tool call can be traced through the cascaded
        # pipeline session's state machine. Log lines prefix [63.1-DIAG] for easy grep.
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

            # Prefetch scheduling data at session init so the availability tools
            # resolve from a warm cache (~50ms of pure slot math) instead of 5
            # live Supabase queries (~500ms) on the call's first availability
            # check — lower caller-perceived latency. Cache is consumed by the
            # availability tools with a 30s TTL; stale reads fall through to the
            # live-fetch path.
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

        # ── Start session (awaited — cascade STT/LLM/TTS plugins initialize) ──
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

        # Phase 66: deterministic opening greeting via session.say(...). The
        # cascaded pipeline speaks a fixed, byte-identical branded greeting
        # (business name + recording disclosure + offer to help) from the
        # src/messages/{en,es}.json templates — no LLM turn consumed, no per-call
        # wording drift. _build_greeting_section in prompt.py tells the model the
        # greeting was already delivered, so it does not re-greet on turn 1.
        #
        # The greeting is made NON-INTERRUPTIBLE by muting the caller's inbound
        # audio for its duration, then unmuting once it has played out. This is
        # echo-defense layer 2 (BVCTelephony is layer 1): the Phase-64 revert
        # showed SIP self-echo can trip the VAD and cut the opening line in half
        # if the input is live during the greeting. Barge-in resumes for the rest
        # of the call the moment the greeting finishes.
        if onboarding_complete:
            greeting_text = _msg(locale, "agent.greeting_onboarding").format(
                business_name=business_name
            )
        else:
            greeting_text = _msg(locale, "agent.greeting_default")

        # Mute caller input so SIP echo / line noise cannot cut the opening line.
        try:
            session.input.set_audio_enabled(False)
        except Exception as e:
            logger.warning(f"[agent] could not mute input before greeting: {e}")

        greeting_handle = None
        try:
            # allow_interruptions=False is a second echo defense behind the input
            # mute above: unlike the realtime model (where it was ignored), the
            # cascade AgentSession honors it (agent_activity.py only resets it for
            # a RealtimeModel). So even if set_audio_enabled(False) ever throws,
            # the greeting still can't be barge-in-cut. Barge-in resumes for the
            # rest of the call (session default allow_interruptions=True).
            greeting_handle = session.say(greeting_text, allow_interruptions=False)
            logger.info(
                "[agent] greeting via session.say (input muted, non-interruptible) "
                "locale=%s onboarding=%s voice_id=%s",
                locale, onboarding_complete, voice_id,
            )
        except Exception as e:
            logger.error(f"[agent] greeting say failed: {e}")

        # Re-enable caller audio once the greeting has fully played out. The
        # GREETING_UNMUTE_TIMEOUT_S cap guarantees input is never left muted if a
        # SIP playout stalls or drops mid-greeting. (If dispatch failed,
        # greeting_handle is None and we unmute immediately.)
        async def _unmute_after_greeting():
            try:
                if greeting_handle is not None:
                    await asyncio.wait_for(
                        greeting_handle.wait_for_playout(),
                        timeout=GREETING_UNMUTE_TIMEOUT_S,
                    )
            except asyncio.TimeoutError:
                logger.warning(
                    "[agent] greeting playout wait timed out at %ss; force-unmuting input",
                    GREETING_UNMUTE_TIMEOUT_S,
                )
            except Exception as e:
                logger.warning(f"[agent] greeting playout wait error: {e}")
            finally:
                try:
                    session.input.set_audio_enabled(True)
                    logger.info("[agent] input unmuted after greeting")
                except Exception as e:
                    logger.warning(f"[agent] could not unmute input after greeting: {e}")

        _greeting_unmute_task = asyncio.create_task(_unmute_after_greeting())

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
