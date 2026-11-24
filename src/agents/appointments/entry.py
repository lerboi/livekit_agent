"""The appointments agent: one call, start to finish.

``src/agent.py`` (the worker entrypoint) has already connected to the room,
waited for the caller and read the room metadata when it hands the call here
(see ``src/agents/__init__.py`` for the selection rule). This module does the
rest: find the business, build the voice pipeline, greet, run the session with
the eight appointment tools, record, and hand over to the post-call pipeline.

What is deliberately absent, compared with the receptionist in ``src/agent.py``:
caller history and CRM context, intake questions, address validation, urgency
triage, lead capture and call transfer. Nothing loads or waits on any of them.

Sandbox rules are unchanged (vault: "Decision - Test calls are sandboxed"):
a test call sends no SMS, writes no CRM rows, is not billed and notifies
nobody; it writes to an external calendar only when it is a Demo Studio call
whose server-set metadata names that calendar.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import sentry_sdk
from livekit import api, rtc
from livekit.agents import (
    Agent,
    AgentSession,
    AudioConfig,
    BackgroundAudioPlayer,
    BuiltinAudioClip,
    ModelSettings,
    room_io,
)
from livekit.plugins import noise_cancellation, silero

from ...calendars import calendar_target, open_calendar
from ...lib.background import create_background_task
from ...lib.demo import publish_event
from ...lib.subscription_gate import is_subscription_blocked
from ...lib.tool_filler import ThinkingSoundController
from ...post_call import run_post_call_pipeline
from ...supabase_client import get_supabase_admin
from ...tools.end_call import _delayed_disconnect
from .guard import guarded_llm_stream
from .pipeline import (
    build_llm,
    build_stt,
    build_turn_detector,
    build_turn_handling,
    resolve_provider,
    resolve_stt_language,
)
from .preamble import goodbye_with_end_call, speak_or_act, strip_tool_preamble
from .speech_filter import strip_tool_mentions
from .prompt import build_prompt, date_context
from .tools import create_tools

logger = logging.getLogger("voco-agent")

AGENT_NAME = "appointments"
# The Demo Studio's "Warm" greeting (test calls only). The standard line is
# two clipped statements, which the voice reads flat; this gives it a
# sentence to open on. It still says the call is recorded.
WARM_GREETING = (
    "Hi, thanks for calling {business_name}! Just so you know, this call is "
    "recorded. How can I help you today?"
)
_ENDING = ("agent_ended", "transferred", "max_duration")


class AppointmentsAgent(Agent):
    def __init__(self, instructions: str, tools: list, deps: dict, turn_policy=None):
        super().__init__(instructions=instructions, tools=tools)
        self._deps = deps
        # Optional: ``turn_policy(deps, chat_ctx)`` returns (tool_choice, nudge)
        # for a turn that must call a tool, or None (src/agents/demo/policy.py).
        self._turn_policy = turn_policy

    async def llm_node(self, chat_ctx, tools, model_settings):
        # Claim guard (guard.py): a reply that says a booking change is done
        # when no tool did it is dropped before it is spoken and run again.
        def _run(tool_choice, nudge):
            ctx = chat_ctx
            if nudge:
                ctx = chat_ctx.copy()
                ctx.add_message(role="system", content=nudge)
            settings = (
                model_settings if tool_choice is None
                else ModelSettings(tool_choice=tool_choice)
            )
            return Agent.default.llm_node(self, ctx, tools, settings)

        if self._turn_policy is not None:
            required = None
            try:
                required = self._turn_policy(self._deps, chat_ctx)
            except Exception as err:  # noqa: BLE001 - a policy must never break a turn
                logger.warning("[appointments] turn policy failed: %s", err)
            if required is not None:
                tool_choice, nudge = required
                logger.info(
                    "[appointments] turn policy requires %s. call=%s",
                    (tool_choice or {}).get("function", {}).get("name"), self._deps.get("call_id"),
                )
                async for chunk in goodbye_with_end_call(
                    strip_tool_mentions(
                        speak_or_act(strip_tool_preamble(_run(tool_choice, nudge), self._deps), self._deps), self._deps,
                    ),
                    self._deps,
                ):
                    yield chunk
                return

        # preamble.py: speech in the same response as a tool call ("Let me
        # check that for you") is dropped, so tools really are called silently.
        # ... and a hang-up with no words gets the call's goodbye line first.
        # speech_filter.py: bracketed notes and tool names are never spoken.
        # speak_or_act: a response that has spoken does not also call a tool.
        async for chunk in goodbye_with_end_call(
            strip_tool_mentions(
                speak_or_act(strip_tool_preamble(guarded_llm_stream(self._deps, _run), self._deps), self._deps), self._deps,
            ),
            self._deps,
        ):
            yield chunk


async def _lookup_tenant(supabase, to_number: str) -> dict | None:
    """The business whose number was called."""
    try:
        resp = await asyncio.to_thread(
            lambda: supabase.table("tenants")
            .select("*")
            .eq("phone_number", to_number)
            .single()
            .execute()
        )
    except Exception as err:  # noqa: BLE001
        logger.warning("[appointments] business lookup failed for %s: %s", to_number, err)
        return None
    return resp.data or None


def _voice_for(base, tenant: dict | None, room_meta: dict, is_test_call: bool) -> str:
    tone = (tenant or {}).get("tone_preset", "professional")
    voice_id = base._resolve_voice((tenant or {}).get("ai_voice"), tone)
    if is_test_call:
        # Admin pages can audition a voice; the value is server-set and
        # checked against an allowlist there. Shape-checked again here.
        override = str(room_meta.get("voice_override") or "").strip()
        if override and re.fullmatch(r"[A-Za-z0-9]{10,40}", override):
            voice_id = override
    return voice_id


async def run_call(
    ctx,
    base,
    *,
    participant,
    room_meta: dict,
    is_test_call: bool,
    is_web_test: bool,
    demo: dict | None,
    to_number: str,
    from_number: str,
    profile: dict | None = None,
) -> None:
    """Run one call. ``base`` is the loaded ``src/agent.py`` module: it carries
    the shared voice tuning, the TTS builder and the greeting messages."""
    # ``profile``: what another agent built on this one supplies
    # (src/agents/demo/entry.py): agent_name, build_prompt, turn_policy,
    # llm (its model when the call names none), keyterms.
    profile = profile or {}
    agent_name = profile.get("agent_name") or AGENT_NAME
    prompt_builder = profile.get("build_prompt") or build_prompt
    call_id = ctx.room.name
    caller_identity = participant.identity or ""
    start_timestamp = int(time.time() * 1000)
    supabase = get_supabase_admin()

    tenant = await _lookup_tenant(supabase, to_number)
    tenant_id = tenant.get("id") if tenant else None
    # A Demo Studio call may answer as a named business (lib/demo.py). `demo`
    # is None on every production call, so this is the tenant's own name there.
    business_name = (
        (demo or {}).get("business_name") or (tenant or {}).get("business_name") or "Voco"
    )
    locale = (tenant or {}).get("default_locale") or "en"
    tenant_timezone = (tenant or {}).get("tenant_timezone") or "UTC"
    logger.info(
        "[appointments] call=%s agent=%s business=%s (%s) test=%s demo=%s",
        call_id, agent_name, tenant_id or "NONE", business_name, is_test_call, demo,
    )

    # Which model runs this call. A test call's server-set metadata may name
    # one (the Demo Studio's Model switch); a production call always uses the
    # worker's setting. The prompt's closing notes follow the model.
    llm_provider = resolve_provider(
        base, (room_meta.get("llm") if is_test_call else None) or profile.get("llm"),
    )

    local_now = datetime.now(tz=ZoneInfo(tenant_timezone))
    system_prompt = prompt_builder(
        business_name=business_name,
        tone_preset=(tenant or {}).get("tone_preset", "professional"),
        working_hours=(tenant or {}).get("working_hours"),
        tenant_timezone=tenant_timezone,
        model=llm_provider,
    ) + "\n\n" + date_context(local_now)

    call_end_reason = ["caller_hangup"]
    may_write_calendar, calendar_provider = calendar_target(is_test_call, demo)

    deps = {
        "agent": agent_name,
        "supabase": supabase,
        "tenant": tenant,
        "tenant_id": tenant_id,
        "tenant_timezone": tenant_timezone,
        "call_id": call_id,
        "call_uuid": None,  # set once the calls row exists
        "from_number": from_number,
        "to_number": to_number,
        "is_test_call": is_test_call,
        "demo": demo,
        "locale": locale,
        "_active_language": None,
        "room_name": call_id,
        "sip_participant_identity": caller_identity,
        "call_end_reason": call_end_reason,
        "ctx": ctx,
        "session": None,
        "_tool_call_log": [],
        # Live `voco.events` stream for the admin pages (web test rooms only).
        "_events_room": ctx.room if is_web_test else None,
        # When the caller last finished speaking: the confirm gates read it.
        "_last_user_turn_at": None,
        # What the caller has said, as (time, text), and whether check_slot
        # must refuse a time the caller never said (tools/availability.py
        # time_not_given; the demo agent turns it on).
        "_caller_texts": [],
        "_ask_for_time": bool(profile.get("ask_for_time")),
        # The calendar this call may write to (None = none, or not yet known).
        "_calendar_provider": calendar_provider,
        "_calendar": None,
        "_calendar_task": None,
        # Spoken before end_call when the model hangs up without a goodbye
        # (preamble.goodbye_with_end_call). English calls only.
        "_goodbye_text": (
            (profile.get("goodbye") or "Thanks for calling {business_name}. Goodbye.").format(
                business_name=business_name,
            )
            if locale == "en" else ""
        ),
    }
    tools = create_tools(deps)

    # ── Calendar connection: opened and token-warmed while the greeting plays ──
    async def _open_calendar():
        if not (may_write_calendar and tenant_id):
            return None
        # Three attempts: a transient network error here would otherwise
        # leave the whole call unable to write to the calendar.
        last_error = None
        for attempt in range(3):
            try:
                calendar = await asyncio.to_thread(open_calendar, supabase, tenant_id, calendar_provider)
                if calendar is None:
                    logger.info(
                        "[appointments] no calendar connected (asked for %s)",
                        calendar_provider or "primary",
                    )
                    return None
                deps["_calendar_provider"] = calendar.provider
                await asyncio.to_thread(calendar.prepare)
                logger.info("[appointments] calendar ready: %s", calendar.provider)
                return calendar
            except Exception as err:  # noqa: BLE001
                last_error = err
                logger.warning("[appointments] calendar open attempt %d failed: %s", attempt + 1, err)
                await asyncio.sleep(0.4 * (attempt + 1))
        logger.error("[appointments] calendar not available: %s", last_error)
        if demo and calendar_provider:
            publish_event(deps, "calendar_failed", appointment_id=None, provider=calendar_provider, action="connect")
        return None

    deps["_calendar_task"] = create_background_task(_open_calendar(), name="calendar-open")

    # ── Voice pipeline (shared tuning: see pipeline.py) ──
    voice_id = _voice_for(base, tenant, room_meta, is_test_call)
    # What speech-to-text listens for. A test call's server-set metadata may
    # say (the Demo Studio's Language switch); otherwise the profile's own
    # setting; otherwise every language, as on production calls.
    stt_language = resolve_stt_language(
        room_meta.get("stt_language") if is_test_call else None, profile.get("stt_language"),
    )
    stt = build_stt(base, [business_name] + list(profile.get("keyterms") or []), stt_language)
    llm, llm_legs = build_llm(base, tenant_id, llm_provider)
    try:
        eleven_key_ok = ctx.proc.userdata.get("eleven_key_ok")
        eleven_voice_ids = ctx.proc.userdata.get("eleven_voice_ids")
        vad = ctx.proc.userdata.get("vad")
    except Exception:  # noqa: BLE001
        eleven_key_ok, eleven_voice_ids, vad = None, None, None
    if is_test_call and eleven_voice_ids is not None and voice_id not in eleven_voice_ids:
        # The account's voice list is read once when the worker process
        # starts. A voice added to the ElevenLabs account since then (to try
        # it from the Demo Studio) is not in that copy: read the list again
        # before _build_tts replaces the voice with the fallback. Test calls
        # only, so a production call never waits on this.
        try:
            fresh = await asyncio.to_thread(base._fetch_elevenlabs_voice_ids)
            if fresh:
                eleven_voice_ids = fresh
                ctx.proc.userdata["eleven_voice_ids"] = fresh
                logger.info(
                    "[appointments] voice list re-read for %s: %s",
                    voice_id, "found" if voice_id in fresh else "still not in the account",
                )
        except Exception as err:  # noqa: BLE001
            logger.warning("[appointments] voice list re-read failed: %s", err)
    # Voice quality and greeting style: the Demo Studio's choices, from the
    # server-set metadata of a test call. A production call gets neither.
    voice_quality = str(room_meta.get("voice_quality") or "").strip().lower() if is_test_call else ""
    if voice_quality not in base.VOICE_QUALITY:
        voice_quality = ""
    greeting_style = str(room_meta.get("greeting") or "").strip().lower() if is_test_call else ""
    tts = base._build_tts(
        voice_id, eleven_key_ok=eleven_key_ok, eleven_voice_ids=eleven_voice_ids,
        quality=voice_quality or None,
    )
    if vad is None:
        vad = silero.VAD.load(min_silence_duration=base.VAD_MIN_SILENCE_S)

    def _log_llm_metrics(provider):
        def _on_metrics(m):
            try:
                logger.info(
                    "[agent] llm_metrics call=%s provider=%s ttft=%.3f prompt_tokens=%d "
                    "cached_tokens=%d completion_tokens=%d cancelled=%s",
                    call_id, provider, m.ttft, m.prompt_tokens, m.prompt_cached_tokens,
                    m.completion_tokens, m.cancelled,
                )
            except Exception:  # noqa: BLE001
                pass
        return _on_metrics

    for provider, leg in llm_legs:
        leg.on("metrics_collected", _log_llm_metrics(provider))

    def _log_tts_metrics(m):
        # One line per spoken sentence. ``s_per_char`` is how long the audio
        # runs for each character of text: when a caller reports a voice that
        # turned slow or slurred, a jump in it says the speech was generated
        # that way, and a steady value says the fault is after this process
        # (network or the listener's device).
        try:
            chars = m.characters_count or 0
            logger.info(
                "[agent] tts_metrics call=%s label=%s ttfb=%.3f gen=%.2f audio=%.2f chars=%d "
                "s_per_char=%.3f cancelled=%s",
                call_id, m.label, m.ttfb, m.duration, m.audio_duration, chars,
                (m.audio_duration / chars) if chars else 0.0, m.cancelled,
            )
        except Exception:  # noqa: BLE001
            pass

    try:
        tts.on("metrics_collected", _log_tts_metrics)
    except Exception:  # noqa: BLE001
        pass

    agent = AppointmentsAgent(
        instructions=system_prompt, tools=tools, deps=deps,
        turn_policy=profile.get("turn_policy"),
    )
    session = AgentSession(
        stt=stt, llm=llm, tts=tts, vad=vad,
        turn_handling=build_turn_handling(base, build_turn_detector(base)),
    )
    deps["session"] = session

    # ── Session events ──
    transcript_turns: list[dict] = []

    @session.on("user_input_transcribed")
    def _on_transcribed(event):
        try:
            if getattr(event, "is_final", False):
                deps["_last_user_turn_at"] = time.time()
                heard = getattr(event, "transcript", None)
                if heard:
                    deps["_caller_texts"].append((time.time(), heard))
                lang = getattr(event, "language", None)
                if lang:
                    deps["_active_language"] = str(lang).lower()
        except Exception:  # noqa: BLE001
            pass

    @session.on("conversation_item_added")
    def _on_item(event):
        text = getattr(event.item, "text_content", None)
        if not text:
            return
        role = "user" if getattr(event.item, "role", None) == "user" else "agent"
        if role == "user":
            deps["_last_user_turn_at"] = time.time()
            deps["_caller_texts"].append((time.time(), text))
        transcript_turns.append({"role": role, "content": text, "timestamp": int(time.time() * 1000)})
        try:
            m = getattr(event.item, "metrics", None) or {}
            logger.info(
                "[agent] turn_metrics call=%s role=%s eot_delay=%s transcription_delay=%s "
                "llm_ttft=%s tts_ttfb=%s",
                call_id, role, m.get("end_of_turn_delay"), m.get("transcription_delay"),
                m.get("llm_node_ttft"), m.get("tts_node_ttfb"),
            )
        except Exception:  # noqa: BLE001
            pass

    @session.on("function_tools_executed")
    def _on_tools(event):
        try:
            for call, out in event.zipped():
                name = getattr(call, "name", "?")
                args = getattr(call, "arguments", None)
                output = (getattr(out, "output", None) if out else None) or ""
                logger.info("[appointments] tool %s(%s) -> %s", name, str(args)[:120], output[:180])
                if deps.get("_events_room") is None:
                    continue
                try:
                    parsed = json.loads(args) if isinstance(args, str) and args else (args or {})
                except Exception:  # noqa: BLE001
                    parsed = {}
                publish_event(
                    deps, "tool",
                    name=name,
                    args=_public_args(parsed),
                    state=output.split("|", 1)[0].strip()[:160],
                    is_error=bool(getattr(out, "is_error", False)) if out else False,
                )
        except Exception as err:  # noqa: BLE001
            logger.warning("[appointments] tool event handling failed: %s", err)

    # ── Mid-call failure recovery and the "are you still there?" prompt ──
    greeting_done = [False]
    recovery_started = [False]
    last_user_state = ["listening"]
    no_input_active = [False]
    error_count = [0]

    async def _speak_and_end(reason: str, message_key: str):
        try:
            call_end_reason[0] = reason
            try:
                handle = session.say(base._msg(locale, message_key), allow_interruptions=False)
                if handle is not None:
                    await asyncio.wait_for(handle.wait_for_playout(), timeout=15)
            except Exception as err:  # noqa: BLE001
                logger.warning("[appointments] recovery line failed (%s); ending anyway", err)
            await _delayed_disconnect(deps)
        except Exception as err:  # noqa: BLE001
            logger.error("[appointments] recovery error (%s); forcing shutdown", err)
            try:
                ctx.shutdown()
            except Exception:  # noqa: BLE001
                pass

    def _begin_recovery(reason: str, message_key: str) -> None:
        if recovery_started[0] or call_end_reason[0] in _ENDING:
            return
        recovery_started[0] = True
        create_background_task(_speak_and_end(reason, message_key))

    async def _no_input_sequence():
        try:
            for _ in range(base.NO_INPUT_MAX_STRIKES):
                if recovery_started[0]:
                    return
                try:
                    handle = session.say(base._msg(locale, "agent.no_input_prompt"), allow_interruptions=True)
                    if handle is not None:
                        await asyncio.wait_for(handle.wait_for_playout(), timeout=10)
                except Exception as err:  # noqa: BLE001
                    logger.warning("[appointments] no-input prompt failed: %s", err)
                await asyncio.sleep(base.NO_INPUT_RESPONSE_WINDOW_S)
                if last_user_state[0] != "away":
                    return
            _begin_recovery("no_input", "agent.no_input_goodbye")
        except Exception as err:  # noqa: BLE001
            logger.warning("[appointments] no-input sequence error: %s", err)
        finally:
            no_input_active[0] = False

    @session.on("error")
    def _on_error(event):
        logger.error("[appointments] session error call=%s: %s", call_id, event.error)
        sentry_sdk.capture_exception(getattr(event.error, "error", event.error))
        try:
            recoverable = getattr(event, "recoverable", None)
            if recoverable is None:
                recoverable = getattr(event.error, "recoverable", True)
            error_count[0] += 1
            if recoverable is False or error_count[0] >= base.ERROR_RECOVERY_THRESHOLD:
                _begin_recovery("error_recovery", "agent.recovery_error")
        except Exception as err:  # noqa: BLE001
            logger.warning("[appointments] error recovery dispatch failed: %s", err)

    @session.on("user_state_changed")
    def _on_user_state(event):
        try:
            new_state = getattr(event, "new_state", None)
            if new_state:
                last_user_state[0] = new_state
            if new_state != "away" or not greeting_done[0]:
                return
            if recovery_started[0] or no_input_active[0] or call_end_reason[0] in _ENDING:
                return
            no_input_active[0] = True
            create_background_task(_no_input_sequence())
        except Exception as err:  # noqa: BLE001
            logger.warning("[appointments] no-input handler error: %s", err)

    # ── Call end: stop the recording, run the post-call pipeline ──
    egress_id = None
    recording_ext = "mp4" if is_test_call else "ogg"
    recording_path = (
        f"{tenant_id}/{call_id}.{recording_ext}" if tenant_id else f"{call_id}.{recording_ext}"
    )
    watchdog_task = None
    background_audio: list = [None]

    async def _on_shutdown(reason: str = ""):
        if watchdog_task is not None and not watchdog_task.done():
            watchdog_task.cancel()
        try:
            if background_audio[0] is not None:
                await asyncio.wait_for(background_audio[0].aclose(), timeout=2.0)
        except Exception:  # noqa: BLE001
            pass
        end_timestamp = int(time.time() * 1000)
        logger.info(
            "[appointments] call ended: %s duration=%ss reason=%s",
            call_id, round((end_timestamp - start_timestamp) / 1000), reason or "unspecified",
        )
        try:
            await db_task
        except Exception:  # noqa: BLE001
            pass
        if egress_id:
            try:
                lk = api.LiveKitAPI()
                try:
                    await lk.egress.stop_egress(api.StopEgressRequest(egress_id=egress_id))
                finally:
                    await lk.aclose()
            except Exception as err:  # noqa: BLE001
                logger.error("[appointments] could not stop the recording: %s", err)
        try:
            # 8 s: the SDK kills the job process 10 s into shutdown.
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
                    "demo": demo,
                    "disconnection_reason": call_end_reason[0],
                    "booking_succeeded": deps.get("_booking_succeeded", False),
                    "booked_appointment_id": deps.get("_booked_appointment_id"),
                    "booked_caller_name": deps.get("_booked_caller_name"),
                    "tool_call_log": deps.get("_tool_call_log", []),
                    # No urgency triage for appointment calls.
                    "skip_triage": True,
                }),
                timeout=8.0,
            )
        except asyncio.TimeoutError:
            logger.error("[appointments] post-call pipeline timed out: %s", call_id)
            sentry_sdk.capture_message(f"Post-call pipeline timeout: callId={call_id}", level="warning")
        except Exception as err:  # noqa: BLE001
            logger.error("[appointments] post-call pipeline error: %s", err)
            sentry_sdk.capture_exception(err)

    ctx.add_shutdown_callback(_on_shutdown)

    # ── Database work that runs under the greeting ──
    async def _run_db_queries():
        if not tenant_id:
            return
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        all_day_floor = (now - timedelta(hours=24)).isoformat()
        call_row = {
            "call_id": call_id,
            "tenant_id": tenant_id,
            "from_number": from_number,
            "to_number": to_number,
            "direction": "inbound",
            "status": "started",
            "start_timestamp": start_timestamp,
            "call_provider": "livekit",
        }
        if is_test_call:
            call_row["is_test_call"] = True

        def _q(build):
            return asyncio.to_thread(lambda: build().execute())

        sub_res, call_res, appts, events, blocks = await asyncio.gather(
            _q(lambda: supabase.table("subscriptions").select("status, current_period_end")
               .eq("tenant_id", tenant_id).eq("is_current", True).limit(1)),
            _q(lambda: supabase.table("calls").upsert(call_row, on_conflict="call_id")),
            _q(lambda: supabase.table("appointments").select("start_time, end_time, zone_id")
               .eq("tenant_id", tenant_id).neq("status", "cancelled").neq("status", "completed")
               .gte("end_time", now_iso)),
            _q(lambda: supabase.table("calendar_events").select("start_time, end_time, is_all_day")
               .eq("tenant_id", tenant_id).gte("end_time", all_day_floor)),
            _q(lambda: supabase.table("calendar_blocks").select("start_time, end_time, is_all_day")
               .eq("tenant_id", tenant_id).gte("end_time", all_day_floor)),
            return_exceptions=True,
        )

        def _rows(result):
            return [] if isinstance(result, Exception) or not result else (result.data or [])

        if not any(isinstance(r, Exception) for r in (appts, events, blocks)):
            # Warm cache for the first availability lookup (30 s TTL).
            deps["_slot_cache"] = {
                "fetched_at": time.time(),
                "appointments": _rows(appts),
                "calendar_events": _rows(events),
                "service_zones": [],
                "zone_travel_buffers": [],
                "calendar_blocks": _rows(blocks),
            }

        if isinstance(call_res, Exception):
            # The calls row carries the transcript, the recording and the
            # test-call flag: try once more before giving up.
            logger.warning("[appointments] call record insert failed, retrying: %s", call_res)
            try:
                call_res = await _q(lambda: supabase.table("calls").upsert(call_row, on_conflict="call_id"))
            except Exception as err:  # noqa: BLE001
                call_res = err
        if isinstance(call_res, Exception):
            logger.error("[appointments] call record insert failed: %s", call_res)
        elif call_res.data:
            deps["call_uuid"] = call_res.data[0].get("id")

        if isinstance(sub_res, Exception):
            logger.warning("[appointments] subscription check failed (allowing call): %s", sub_res)
            return
        sub = sub_res.data[0] if sub_res.data else None
        if not (sub and is_subscription_blocked(sub.get("status"), sub.get("current_period_end"))):
            return
        if is_test_call:
            logger.info("[appointments] subscription blocked, test call allowed: tenant=%s", tenant_id)
            return
        logger.info("[appointments] subscription blocked: tenant=%s; disconnecting", tenant_id)
        call_end_reason[0] = "subscription_blocked"
        try:
            lk = api.LiveKitAPI()
            try:
                await lk.room.remove_participant(
                    api.RoomParticipantIdentity(room=call_id, identity=caller_identity)
                )
            finally:
                await lk.aclose()
        except Exception as err:  # noqa: BLE001
            logger.error("[appointments] could not disconnect blocked caller: %s", err)

    db_task = create_background_task(_run_db_queries(), name="appointments-db")

    # ── Start ──
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
    logger.info("[appointments] session started: %s", call_id)

    # Tell the admin page which model this call is really on (web test rooms).
    _model_names = {"groq": base.GROQ_LLM_MODEL, "openai": base.LLM_MODEL}
    publish_event(
        deps, "session",
        agent=agent_name,
        voice_quality=voice_quality or "fast",
        stt_language=stt_language,
        llm=llm_legs[0][0],
        model=_model_names.get(llm_legs[0][0]),
        fallback=_model_names.get(llm_legs[1][0]) if len(llm_legs) > 1 else None,
    )

    # The keyboard sound that covers a tool's wait. The Demo Studio can turn it
    # off for a recording (server-set `typing_sound: false`, test calls only).
    typing_sound = base.THINKING_SOUND_ENABLED and not (
        is_test_call and room_meta.get("typing_sound") is False
    )

    async def _start_background_audio():
        if not (typing_sound or base.AMBIENT_SOUND_ENABLED):
            return
        try:
            ambient = (
                AudioConfig(BuiltinAudioClip.OFFICE_AMBIENCE, volume=base.AMBIENT_SOUND_VOLUME)
                if base.AMBIENT_SOUND_ENABLED else None
            )
            player = BackgroundAudioPlayer(ambient_sound=ambient)
            await player.start(room=ctx.room, agent_session=session)
            background_audio[0] = player
            if typing_sound:
                clips = [
                    AudioConfig(clip, volume=base.THINKING_SOUND_VOLUME, probability=0.5)
                    for clip in (BuiltinAudioClip.KEYBOARD_TYPING, BuiltinAudioClip.KEYBOARD_TYPING2)
                ]
                controller = ThinkingSoundController(player, clips)
                controller.attach(session)
                deps["_thinking_ctl"] = controller
        except Exception as err:  # noqa: BLE001
            logger.warning("[appointments] background audio unavailable (%s)", err)

    create_background_task(_start_background_audio(), name="background-audio")

    # ── Greeting: fixed line, caller muted while it plays (echo defence) ──
    greeting_text = (
        base._msg(locale, "agent.greeting_onboarding").format(business_name=business_name)
        if tenant else base._msg(locale, "agent.greeting_default")
    )
    if tenant and locale == "en" and profile.get("greeting"):
        # An agent built on this one may have its own opening line.
        greeting_text = profile["greeting"].format(business_name=business_name)
    if tenant and greeting_style == "warm" and locale == "en":
        greeting_text = WARM_GREETING.format(business_name=business_name)
    try:
        session.input.set_audio_enabled(False)
    except Exception as err:  # noqa: BLE001
        logger.warning("[appointments] could not mute input before the greeting: %s", err)
    greeting_handle = None
    try:
        greeting_handle = session.say(greeting_text, allow_interruptions=False)
    except Exception as err:  # noqa: BLE001
        logger.error("[appointments] greeting failed: %s", err)

    # Warm each model's prompt cache with this call's exact prompt and tools.
    if base.LLM_CACHE_WARM:
        try:
            for provider, leg in llm_legs:
                warm_ctx = agent.chat_ctx.copy()
                if provider != "openai":
                    warm_ctx.add_message(role="user", content="Hello")  # Groq rejects a chat with no user turn
                create_background_task(
                    base._warm_prompt_cache(
                        leg, warm_ctx, agent.tools,
                        timeout_s=base.LLM_CACHE_WARM_TIMEOUT_S,
                        call_id=f"{call_id} provider={provider}",
                    ),
                    name=f"llm-cache-warm-{provider}",
                )
        except Exception as err:  # noqa: BLE001
            logger.warning("[appointments] prompt cache warm not started: %s", err)

    async def _unmute_after_greeting():
        try:
            if greeting_handle is not None:
                await asyncio.wait_for(
                    greeting_handle.wait_for_playout(), timeout=base.GREETING_UNMUTE_TIMEOUT_S,
                )
        except Exception as err:  # noqa: BLE001
            logger.warning("[appointments] greeting playout wait: %r", err)
        try:
            session.input.set_audio_enabled(True)
        except Exception as err:  # noqa: BLE001
            logger.warning("[appointments] could not unmute input: %s", err)
        greeting_done[0] = True

    create_background_task(_unmute_after_greeting(), name="greeting-unmute")

    # ── Call-length cap ──
    async def _watchdog():
        try:
            elapsed = time.time() - start_timestamp / 1000.0
            if base.WRAP_UP_CALL_SECONDS < base.MAX_CALL_SECONDS:
                await asyncio.sleep(max(0.0, base.WRAP_UP_CALL_SECONDS - elapsed))
                try:
                    nudge = agent.chat_ctx.copy()
                    nudge.add_message(
                        role="system",
                        content=(
                            f"TIME LIMIT: this call has run for {base.WRAP_UP_CALL_SECONDS // 60} "
                            "minutes and will be disconnected shortly. Finish what the caller "
                            "asked for and move to your goodbye."
                        ),
                    )
                    await agent.update_chat_ctx(nudge)
                except Exception as err:  # noqa: BLE001
                    logger.warning("[appointments] wrap-up nudge failed: %s", err)
            elapsed = time.time() - start_timestamp / 1000.0
            await asyncio.sleep(max(0.0, base.MAX_CALL_SECONDS - elapsed))
            call_end_reason[0] = "max_duration"
            try:
                handle = session.say(base._msg(locale, "agent.max_duration_goodbye"), allow_interruptions=False)
                await asyncio.wait_for(handle.wait_for_playout(), timeout=20)
            except Exception as err:  # noqa: BLE001
                logger.warning("[appointments] max-duration goodbye failed: %s", err)
            await _delayed_disconnect(deps)
        except asyncio.CancelledError:
            pass
        except Exception as err:  # noqa: BLE001
            logger.error("[appointments] watchdog error: %s", err)

    watchdog_task = create_background_task(_watchdog(), name="call-watchdog")

    # ── Recording ──
    async def _start_recording():
        nonlocal egress_id
        await db_task
        try:
            lk = api.LiveKitAPI()
            try:
                info = await lk.egress.start_room_composite_egress(
                    api.RoomCompositeEgressRequest(
                        room_name=call_id,
                        audio_only=True,
                        file_outputs=[api.EncodedFileOutput(
                            file_type=api.EncodedFileType.MP4 if is_test_call else api.EncodedFileType.OGG,
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
                egress_id = info.egress_id
            finally:
                await lk.aclose()
            if deps.get("call_uuid"):
                await asyncio.to_thread(
                    lambda: supabase.table("calls").update({
                        "egress_id": egress_id,
                        "recording_storage_path": recording_path,
                    }).eq("call_id", call_id).execute()
                )
        except Exception as err:  # noqa: BLE001
            logger.error("[appointments] could not start the recording: %s", err)
            # Tell the admin page why there will be no audio to download. On
            # 2026-11-25 LiveKit refused every recording with "egress minutes
            # exceeded" (the plan's monthly quota) and the page could only say
            # "No recording available".
            text = str(err).lower()
            publish_event(
                deps, "recording_failed",
                reason="quota" if ("egress minutes" in text or "resource_exhausted" in text) else "error",
            )

    create_background_task(_start_recording(), name="recording-start")


_HIDDEN_ARGS = ("slot_token", "appointment_token")


def _public_args(args) -> dict:
    """Tool arguments for the live event stream, without the opaque tokens."""
    if not isinstance(args, dict):
        return {}
    return {k: v for k, v in args.items() if k not in _HIDDEN_ARGS}
