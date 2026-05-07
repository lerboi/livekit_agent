"""
Shared helpers for the three availability tools: check_slot, check_day,
next_available_days. Extracted from the former monolithic check_availability.py
so each narrow tool imports only what it needs and payload/description sizes
stay small for Gemini 3.1 Flash Live (see
.planning/research/check-availability-split-plan.md).

Nothing in this module is a tool — tools live in their own files and call
into here. No behavior change from the pre-split implementation.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..lib.slot_calculator import calculate_available_slots
from ..utils import to_local_date_string, format_zone_pair_buffers

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Input-mute during tool execution.
#
# Gemini 3.1 Flash Live only supports BLOCKING function calling
# (https://ai.google.dev/gemini-api/docs/live-tools: "Asynchronous function
# calling is not yet supported in Gemini 3.1 Flash Live. The model will not
# start responding until you've sent the tool response."). When the caller
# speaks during the BLOCKING wait, the Live guide documents the failure mode
# explicitly: "When VAD detects an interruption, the ongoing generation is
# canceled and discarded... The Gemini server then discards any pending
# function calls." Our tool response then lands on a cancelled call and
# produces no audio output — the session stalls.
#
# Mitigation: mute the caller's input stream (LiveKit client-side) for the
# duration of the tool call + post-tool response window. Same mechanism as
# Phase 63.1-07 greeting-mute (session.input.set_audio_enabled). This is a
# client-side detachment of the audio stream — no session config mutation,
# so it works on 3.1 despite mutable_chat_context=False et al.
# ─────────────────────────────────────────────────────────────────────────────
#
# Phase 61.2 Fix B: fallback raised 15→25s. The booking-section name+address
# readback can run 10-14s; on a server-cancelled tool call, the recovery
# generation may extend beyond that. 15s left no margin and the safety
# unmute fired mid-recovery (call AJ_vV4DM5AG9t7W). 25s is the new ceiling.
_TOOL_MUTE_FALLBACK_S = 25.0


def mute_input_during_tool(deps: dict, fallback_s: float = _TOOL_MUTE_FALLBACK_S) -> None:
    """Mute caller input for the tool call AND the full post-tool response.

    Unmutes when the agent transitions `speaking → listening` AFTER the
    post-tool response has started (i.e. after we've seen a fresh
    `*→speaking` transition following the mute), or after `fallback_s`
    seconds — whichever first.

    Why event-based instead of a fixed timer: the booking-section prompt
    requires a name+address readback after a successful check_slot, so the
    post-tool response regularly runs 10-14s. A short fixed timer (e.g. 5s)
    expired mid-response, the caller's voice during the readback then
    triggered Gemini's server VAD, which cancelled the in-flight generation
    mid-utterance — the visible "agent keeps cutting herself off" symptom
    plus `_SegmentSynchronizerImpl.playback_finished … text_done=false`
    warnings in production logs.

    The 15s fallback is a safety cap: if Gemini's response never starts
    (e.g. function call orphaned by an earlier VAD cancel) we don't trap
    the caller permanently.

    Counter `_tool_mute_id` prevents a stale unmute (from an earlier tool
    call) from racing a newer mute.
    """
    session = deps.get("session")
    if not session:
        return

    mute_id = deps.get("_tool_mute_id", 0) + 1
    deps["_tool_mute_id"] = mute_id

    try:
        session.input.set_audio_enabled(False)
        # Phase 61.3 D-04: capture mute timestamp so the fallback branch can
        # detect stall (no audio frames advanced during speaking window).
        # Closure-captured by _unmute_logic — read-only there, no nonlocal needed.
        mute_set_at_ms = int(_time.time() * 1000)
        logger.info("[tool_mute] muted input id=%d fallback=%.1fs", mute_id, fallback_s)
    except Exception as e:
        logger.warning("[tool_mute] failed to mute: %s", e)
        return

    # State tracker for the listener closure. Lists are used so the closure
    # can mutate without `nonlocal` (Python 3.x quirk: nonlocal in callbacks
    # registered into a non-async event emitter).
    saw_fresh_speaking = [False]
    unmute_event = asyncio.Event()

    def _on_state_change(event):
        try:
            new_state = getattr(event, "new_state", None)
            old_state = getattr(event, "old_state", None)
            # A transition INTO speaking from any non-speaking state = start
            # of the post-tool response. The agent's path during a tool call
            # is `listening → thinking → speaking → listening` (the
            # `thinking` state surfaces from Gemini's thinking_config or
            # server-side state reporting during tool execution). Any pre-mute
            # filler in flight at registration time has old_state == "speaking",
            # so its trailing `speaking → listening` doesn't satisfy this gate.
            #
            # Pre-fix this matched only `listening → speaking`, missing the
            # `thinking → speaking` path — call AJ_bFP3MLdqnKqT (2026-05-07)
            # held the mute for the full 25s fallback on every tool call
            # because saw_fresh_speaking[0] never flipped True.
            if new_state == "speaking" and old_state != "speaking":
                saw_fresh_speaking[0] = True
            elif (
                old_state == "speaking"
                and new_state == "listening"
                and saw_fresh_speaking[0]
            ):
                # Post-tool response audio is done — caller can talk again.
                unmute_event.set()
        except Exception:
            pass

    session.on("agent_state_changed", _on_state_change)

    def _on_tools_executed(event):
        # Phase 61.2 Fix B: a fresh tool execution during the mute window
        # means we are inside a recovery generation step (Gemini retried after
        # a server cancel). Reset the listener so the unmute waits for the
        # NEW generation's clean speak/listen cycle, not the cancelled one.
        try:
            saw_fresh_speaking[0] = False
        except Exception:
            pass
        # Phase 61.3 D-05: capture last function-call call_id + name for the
        # cascade-recovery replay path (consumed by _attempt_tool_result_replay
        # added in Plan 03). FunctionToolsExecutedEvent.function_calls is a
        # list[FunctionCall]; use the most recent entry. Best-effort — null
        # guards in the replay helper handle missing keys.
        try:
            fcs = getattr(event, "function_calls", None) or []
            if fcs:
                last_fc = fcs[-1]
                deps["_last_tool_call_id"] = getattr(last_fc, "call_id", None)
                deps["_last_tool_name"] = getattr(last_fc, "name", None)
        except Exception:
            pass

    session.on("function_tools_executed", _on_tools_executed)

    async def _unmute_logic():
        try:
            await asyncio.wait_for(unmute_event.wait(), timeout=fallback_s)
            unmute_reason = "agent finished speaking"
        except asyncio.TimeoutError:
            unmute_reason = f"fallback timeout {fallback_s:.1f}s"
            # Phase 61.3 D-03/D-05/D-06: stall-detection + best-effort tool-result
            # replay BEFORE listener cleanup and BEFORE set_audio_enabled(True).
            # Closes the slot-hallucination cascade (call AJ_5NcSoiaZGZTJ).
            #
            # Phase 61.3-amend: pass `saw_fresh_speaking[0]` as the primary
            # stall signal. The audio-frame check alone produced a false-negative
            # in call AJ_b8ACLgXZ4XZA (2026-05-07): residual frames from the
            # pre-mute filler stamped `last_audio_frame_at` ~15ms AFTER
            # `mute_set_at_ms`, so `stall_confirmed` was False and recovery was
            # silently skipped. The state-change flag is a clean monotonic
            # signal — True iff Gemini has actually started a new speak turn
            # AFTER mute.
            await _attempt_tool_result_replay(
                deps, session, mute_set_at_ms, saw_fresh_speaking[0]
            )

        # Best-effort listener removal. AgentSession is a pyee EventEmitter
        # subclass; both `off` and `remove_listener` are accepted depending
        # on version.
        try:
            if hasattr(session, "off"):
                session.off("agent_state_changed", _on_state_change)
                session.off("function_tools_executed", _on_tools_executed)
            elif hasattr(session, "remove_listener"):
                session.remove_listener("agent_state_changed", _on_state_change)
                session.remove_listener("function_tools_executed", _on_tools_executed)
        except Exception:
            pass

        if deps.get("_tool_mute_id") == mute_id:
            try:
                session.input.set_audio_enabled(True)
                logger.info("[tool_mute] unmuted input id=%d (%s)", mute_id, unmute_reason)
            except Exception as e:
                logger.warning("[tool_mute] failed to unmute: %s", e)
        else:
            logger.info("[tool_mute] skip unmute id=%d — superseded by id=%d (%s)",
                        mute_id, deps.get("_tool_mute_id"), unmute_reason)

    asyncio.create_task(_unmute_logic())


async def _attempt_tool_result_replay(
    deps: dict,
    session,
    mute_set_at_ms: int,
    saw_fresh_speaking: bool = False,
) -> None:
    """Phase 61.3 cascade-recovery: replay the last tool's STATE+DIRECTIVE
    string as a synthetic FunctionCallOutput via update_chat_ctx after a
    confirmed Gemini-server stall.

    Triggered from _unmute_logic()'s TimeoutError branch when the 25s
    fallback fires. Stall is confirmed when the agent has NOT freshly
    transitioned listening→speaking after mute (D-04). Recovery fires
    BEFORE the input unmute (D-06). All actions are best-effort (D-07):
    any failure logs and increments stalled_generation_replay_failed.

    The tool_results send at realtime_api.py:637-638 is unconditional —
    not gated on mutable_chat_context — so this works on
    gemini-3.1-flash-live-preview despite mutable_chat_context=False.

    See 61.3-RESEARCH.md § 1, § 2, § 5, § 6 for accessor + API shape.

    Phase 61.3-amend (call AJ_b8ACLgXZ4XZA, 2026-05-07): the original
    predicate `last_audio_frame_at <= mute_set_at_ms` produced a
    false-negative when the agent finished a "let me check…" filler
    in the 15-30ms after `mute_set_at_ms`. Residual frames from that
    filler stamped `last_audio_frame_at` AFTER mute → stall_confirmed
    became False → recovery silently skipped → caller heard the cascade.
    Fix: `saw_fresh_speaking` (closure flag from `_on_state_change`)
    is the truth source. The audio-frame check is retained with a 250ms
    grace as belt-and-braces — both must indicate quiescence to confirm
    a stall.
    """
    diag = deps.get("_diag_record")

    # D-04 (61.3-amend): stall confirmation — no fresh agent speak transition
    # AND no audio frames during the mute window (with grace for filler residue).
    GRACE_MS = 250
    last_frame_ms = diag[0].get("last_audio_frame_at") if diag else None
    audio_quiescent = (
        last_frame_ms is None or last_frame_ms <= mute_set_at_ms + GRACE_MS
    )
    stall_confirmed = (not saw_fresh_speaking) and audio_quiescent
    if not stall_confirmed:
        # Either Gemini freshly entered a speak turn after mute (saw_fresh_speaking)
        # or audio frames advanced past the residual-filler grace window —
        # this is not the cascade failure mode 61.3 targets. Skip replay.
        return

    # Pull last tool's STATE string + call_id + name (populated by Plan 01).
    state_str = deps.get("_last_tool_state")
    call_id = deps.get("_last_tool_call_id")
    tool_name = deps.get("_last_tool_name")
    if not (state_str and call_id and tool_name):
        # No tool result available to replay — skip silently.
        return

    # D-08 conditional-emit: increment recovery-attempt counter.
    if diag:
        diag[0]["stalled_generation_recoveries"] = (
            diag[0].get("stalled_generation_recoveries", 0) + 1
        )

    try:
        # D-05: accessor chain to the underlying RealtimeSession.
        # session._activity may be None if the session is closing.
        rt_session = None
        if session._activity is not None:
            rt_session = session._activity.realtime_llm_session
        if rt_session is None:
            raise RuntimeError("no active rt_session for replay")

        # D-05: construct the synthetic FunctionCallOutput.
        # Local import keeps module load light and avoids circular import risk.
        from livekit.agents import llm as _llm
        synthetic_output = _llm.FunctionCallOutput(
            call_id=call_id,
            name=tool_name,
            output=state_str,
            is_error=False,
        )

        # D-05: send via update_chat_ctx — the tool_results path at
        # realtime_api.py:637-638 sends unconditionally.
        chat_ctx = rt_session.chat_ctx.copy()
        chat_ctx.items.append(synthetic_output)
        await rt_session.update_chat_ctx(chat_ctx)

        logger.info(
            "[tool_mute] stall-recovery replay sent id=%d tool=%s state=%.80s",
            deps.get("_tool_mute_id", 0), tool_name, state_str,
        )

    except Exception as e:
        # D-07 best-effort: log + increment failure counter, do not
        # block the call on replay failure.
        logger.warning("[tool_mute] stall-recovery replay failed: %s", e)
        if diag:
            diag[0]["stalled_generation_replay_failed"] = (
                diag[0].get("stalled_generation_replay_failed", 0) + 1
            )


# ─────────────────────────────────────────────────────────────────────────────
# Slot-cache + slot-token constants (unchanged from pre-split).
# deps["_slot_cache"] TTL narrows the check_slot / check_day live-fetch window
# so Gemini Live's server-side VAD has less time to cancel the tool call.
# deps["_slot_tokens"] maps an opaque "slot_xxxxxxxx" to UTC (start, end) so
# book_appointment can resolve without trusting any Gemini-reconstructed ISO.
# ─────────────────────────────────────────────────────────────────────────────
SLOT_CACHE_TTL_S = 30.0
SLOT_TOKEN_TTL_S = 600.0


# ─────────────────────────────────────────────────────────────────────────────
# Slot-token registry
# ─────────────────────────────────────────────────────────────────────────────

def register_slot_token(deps: dict, slot_start: str, slot_end: str) -> str:
    """Mint an opaque token bound to a (slot_start_utc, slot_end_utc) pair.
    8 hex chars = 32 bits, collision-proof for the ~10 tokens a call produces."""
    token = "slot_" + secrets.token_hex(4)
    tokens = deps.setdefault("_slot_tokens", {})
    tokens[token] = {
        "slot_start_utc": slot_start,
        "slot_end_utc": slot_end,
        "created_at": _time.time(),
    }
    return token


# ─────────────────────────────────────────────────────────────────────────────
# Date / time formatting
# ─────────────────────────────────────────────────────────────────────────────

def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def format_date_label(date_str: str, tenant_timezone: str) -> str:
    """'2026-04-28' + 'America/Chicago' -> 'Tuesday, April 28th'."""
    year, month, day = (int(x) for x in date_str.split("-"))
    dt = datetime(year, month, day, 12, 0, 0, tzinfo=ZoneInfo(tenant_timezone))
    return f"{dt.strftime('%A')}, {dt.strftime('%B')} {_ordinal(dt.day)}"


def parse_hhmm_to_utc(time_str: str, date_str: str, tenant_timezone: str) -> datetime | None:
    """Parse 'HH:MM' (24-hour) for the given YYYY-MM-DD date in the tenant's
    timezone; return the instant as UTC. Returns None on malformed input.

    Schema-enforced pattern (check_slot raw_schema) means this only needs to
    handle the HH:MM shape, but we retain belt-and-braces 12-hour parsing in
    case Gemini bypasses the schema — rare, but the audit noted Phase 63.1-08/
    09/10 all fought schema-vs-prose drift.
    """
    s = time_str.strip().lower()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
    else:
        m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
        if not m:
            return None
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        ampm = m.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    year, month, day = (int(x) for x in date_str.split("-"))
    local_dt = datetime(year, month, day, hour, minute, 0, tzinfo=ZoneInfo(tenant_timezone))
    return local_dt.astimezone(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Tenant + scheduling data fetch
# ─────────────────────────────────────────────────────────────────────────────

_NEEDED_TENANT_FIELDS = (
    "tenant_timezone",
    "working_hours",
    "slot_duration_mins",
    "business_name",
)


async def ensure_tenant(deps: dict) -> dict | None:
    """Return the tenant dict. Prefers the session-init fetch already on deps;
    re-queries only when the cached value is missing a required field."""
    tenant = deps.get("tenant")
    if tenant and all(k in tenant for k in _NEEDED_TENANT_FIELDS):
        return tenant

    tenant_id = deps.get("tenant_id")
    supabase = deps["supabase"]
    if not tenant_id:
        return None

    try:
        result = await asyncio.to_thread(
            lambda: supabase.table("tenants")
            .select("tenant_timezone, working_hours, slot_duration_mins, business_name")
            .eq("id", tenant_id)
            .single()
            .execute()
        )
        return result.data if result.data else None
    except Exception as e:
        logger.error("[availability] tenant config fetch failed: %s", e)
        return None


async def fetch_scheduling_data(deps: dict) -> dict | None:
    """Return {appointments, calendar_events, service_zones, zone_travel_buffers,
    calendar_blocks} from the prefetched slot_cache when fresh; otherwise
    live-fetch in parallel and refresh the cache. Returns None on fetch failure.

    Shared by all three availability tools so cache invalidation
    (book_appointment clears deps["_slot_cache"]) affects all of them.
    """
    tenant_id = deps.get("tenant_id")
    supabase = deps["supabase"]
    if not tenant_id:
        return None

    cache = deps.get("_slot_cache")
    if cache and (_time.time() - cache.get("fetched_at", 0)) < SLOT_CACHE_TTL_S:
        logger.info(
            "[availability] slot_cache hit age=%.1fs appts=%d events=%d zones=%d buffers=%d blocks=%d",
            _time.time() - cache.get("fetched_at", 0),
            len(cache.get("appointments") or []),
            len(cache.get("calendar_events") or []),
            len(cache.get("service_zones") or []),
            len(cache.get("zone_travel_buffers") or []),
            len(cache.get("calendar_blocks") or []),
        )
        return {
            "appointments": cache.get("appointments") or [],
            "calendar_events": cache.get("calendar_events") or [],
            "service_zones": cache.get("service_zones") or [],
            "zone_travel_buffers": cache.get("zone_travel_buffers") or [],
            "calendar_blocks": cache.get("calendar_blocks") or [],
        }

    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        appts_r, events_r, zones_r, buffers_r, blocks_r = await asyncio.gather(
            asyncio.to_thread(
                lambda: supabase.table("appointments")
                .select("start_time, end_time, zone_id")
                .eq("tenant_id", tenant_id)
                .neq("status", "cancelled")
                .neq("status", "completed")
                .gte("end_time", now_iso)
                .execute()
            ),
            asyncio.to_thread(
                lambda: supabase.table("calendar_events")
                .select("start_time, end_time")
                .eq("tenant_id", tenant_id)
                .gte("end_time", now_iso)
                .execute()
            ),
            asyncio.to_thread(
                lambda: supabase.table("service_zones")
                .select("id, name, postal_codes")
                .eq("tenant_id", tenant_id)
                .execute()
            ),
            asyncio.to_thread(
                lambda: supabase.table("zone_travel_buffers")
                .select("zone_a_id, zone_b_id, buffer_mins")
                .eq("tenant_id", tenant_id)
                .execute()
            ),
            asyncio.to_thread(
                lambda: supabase.table("calendar_blocks")
                .select("start_time, end_time")
                .eq("tenant_id", tenant_id)
                .gte("end_time", now_iso)
                .execute()
            ),
        )
    except Exception as e:
        logger.error("[availability] scheduling data fetch failed: %s", e)
        return None

    sched = {
        "appointments": appts_r.data or [],
        "calendar_events": events_r.data or [],
        "service_zones": zones_r.data or [],
        "zone_travel_buffers": buffers_r.data or [],
        "calendar_blocks": blocks_r.data or [],
    }
    deps["_slot_cache"] = {"fetched_at": _time.time(), **sched}
    return sched


# ─────────────────────────────────────────────────────────────────────────────
# Slot math
# ─────────────────────────────────────────────────────────────────────────────

def calc_slots_for_dates(
    tenant: dict,
    dates: list[str],
    sched: dict,
    tenant_timezone: str,
) -> list[dict]:
    """Flatten calculate_available_slots() across one or more dates."""
    slot_duration = tenant.get("slot_duration_mins") or 60
    all_slots: list[dict] = []
    for date_str in dates:
        day_slots = calculate_available_slots(
            working_hours=tenant.get("working_hours") or {},
            slot_duration_mins=slot_duration,
            existing_bookings=sched["appointments"],
            external_blocks=sched["calendar_events"] + sched["calendar_blocks"],
            zones=sched["service_zones"],
            zone_pair_buffers=format_zone_pair_buffers(sched["zone_travel_buffers"]),
            target_date=date_str,
            tenant_timezone=tenant_timezone,
            max_slots=50,
        )
        all_slots.extend(day_slots)
    return all_slots


def next_n_local_dates(n: int, tenant_timezone: str) -> list[str]:
    """Return the next N calendar dates (including today) as YYYY-MM-DD strings
    in the tenant's local timezone."""
    now = datetime.now(timezone.utc)
    return [to_local_date_string(now + timedelta(days=i), tenant_timezone) for i in range(n)]


def tenant_today(tenant_timezone: str) -> str:
    """Today's date (YYYY-MM-DD) in tenant local time."""
    return to_local_date_string(datetime.now(timezone.utc), tenant_timezone)


# ─────────────────────────────────────────────────────────────────────────────
# Tool-call log (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def log_tool_call(deps: dict, entry: dict[str, Any]) -> None:
    """Append one entry to deps['_tool_call_log']. Used by post-call pipeline
    for silent hallucination detection. ts is added if not provided."""
    entry.setdefault("ts", datetime.now(timezone.utc).isoformat())
    deps.setdefault("_tool_call_log", []).append(entry)
