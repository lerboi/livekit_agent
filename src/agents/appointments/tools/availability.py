"""Availability tools: check_slot, check_day, next_available_days.

Every time the agent speaks comes from one of these returns. Each bookable
time carries a ``slot_token``; ``book_appointment`` and
``reschedule_appointment`` accept nothing else, so the model can never book a
time it made up.

Slot arithmetic and the slot cache are the shared ones in
``src/tools/_availability_lib`` and ``src/lib/slot_calculator``. Two things
differ from the receptionist's tools: appointments here need no travel time
between them, and a failed lookup never points at lead capture (this agent has
no such tool).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from livekit.agents import RunContext, function_tool

from ....lib.tool_filler import tool_filler
from ....tools import _availability_lib as lib
from .common import LOOKUP_FILLER_DELAY_S, TRY_AGAIN, local_hour, say_time, state, tz_of

logger = logging.getLogger("voco-agent")

LOOKAHEAD_DAYS = 5
OPTIONS_PER_DAY = 2
MIN_NOTICE = timedelta(seconds=lib.MIN_NOTICE_TODAY_S)

# Local hour ranges [from, to) for a caller's "morning / afternoon / evening".
_PARTS_OF_DAY = {"morning": (0, 12), "afternoon": (12, 17), "evening": (17, 24)}

_CHECK_SLOT = {
    "name": "check_slot",
    "description": (
        "Check whether one specific date and time is free. Call it every time "
        "the caller names a concrete time. Call it directly, without announcing "
        "it. The return is a state+directive string; do not read it aloud."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "pattern": r"^\d{4}-\d{2}-\d{2}$",
                "description": "Date as YYYY-MM-DD in the business's local timezone.",
            },
            "time": {
                "type": "string",
                "pattern": r"^([01]?\d|2[0-3]):[0-5]\d$",
                "description": (
                    "Time as HH:MM, 24-hour, in the business's local timezone. "
                    "Two in the afternoon is 14:00, not 2:00."
                ),
            },
        },
        "required": ["date", "time"],
    },
}

_CHECK_DAY = {
    "name": "check_day",
    "description": (
        "List open times on one day. Use it when the caller names a day but no "
        "exact time. Pass part_of_day when they said morning, afternoon or "
        "evening. Returns two open times, each with a slot_token. Call "
        "it directly, without announcing it. The return is a state+directive "
        "string; do not read it aloud."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "pattern": r"^\d{4}-\d{2}-\d{2}$",
                "description": "Date as YYYY-MM-DD in the business's local timezone.",
            },
            "part_of_day": {
                "type": "string",
                "enum": ["morning", "afternoon", "evening"],
                "description": "Only if the caller asked for that part of the day.",
            },
        },
        "required": ["date"],
    },
}

_NEXT_DAYS = {
    "name": "next_available_days",
    "description": (
        "Find which of the next few days have open times. Use it only when the "
        "caller has no day in mind. Then call check_day for the day they pick. "
        "Call it directly, without announcing it. The return is a "
        "state+directive string; do not read it aloud."
    ),
    "parameters": {"type": "object", "properties": {}},
}


# ── shared lookups ─────────────────────────────────────────────────────────

async def _context(deps: dict):
    """(tenant, tz, sched), or an error state string."""
    if not deps.get("tenant_id"):
        return state(deps, f"STATE:lookup_failed reason=no_business | DIRECTIVE:{TRY_AGAIN}.")
    tenant = await lib.ensure_tenant(deps)
    if not tenant:
        return state(deps, f"STATE:lookup_failed reason=business | DIRECTIVE:{TRY_AGAIN}.")
    sched = await lib.fetch_scheduling_data(deps)
    if sched is None:
        return state(deps, f"STATE:lookup_failed reason=calendar | DIRECTIVE:{TRY_AGAIN}.")
    # No travel time between appointments, no service zones.
    tenant = {**tenant, "travel_buffer_mins": 0}
    sched = {**sched, "service_zones": [], "zone_travel_buffers": []}
    return tenant, tz_of(deps), sched


def _past_date(deps: dict, date: str, today: str) -> str:
    """A date before today is almost always the model miscounting, not the
    caller asking for the past: send it back to the date list, silently."""
    return state(
        deps,
        f"STATE:past_date requested={date} today={today}"
        " | DIRECTIVE:that date is in the past, so it is not the day the caller"
        " meant. Do not mention this to the caller. Read the right date from the"
        " date list in your instructions and call the tool again now.",
    )


def open_slots(tenant: dict, date: str, sched: dict, tz: str) -> list[dict]:
    return sorted(
        lib.calc_slots_for_dates(tenant, [date], sched, tz),
        key=lambda s: s.get("start") or "",
    )


def _offer(deps: dict, slot: dict, tz: str) -> tuple[str, str]:
    return say_time(slot["start"], tz), lib.register_slot_token(deps, slot["start"], slot["end"])


def next_opening(deps: dict, tenant: dict, sched: dict, tz: str, after_date: str, days: int = 3):
    """(speech, token) for the first open time in the days after ``after_date``."""
    try:
        base = datetime.strptime(after_date, "%Y-%m-%d")
    except ValueError:
        return None
    for offset in range(1, days + 1):
        slots = open_slots(tenant, (base + timedelta(days=offset)).strftime("%Y-%m-%d"), sched, tz)
        if slots:
            speech, token = _offer(deps, slots[0], tz)
            deps["_last_offered_token"] = token
            return speech, token
    return None


def _start_dt(slot: dict) -> datetime:
    return lib._slot_start_dt_utc(slot)


# ── check_slot ─────────────────────────────────────────────────────────────

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _minutes(hhmm) -> int | None:
    match = _HHMM_RE.match(str(hhmm or "").strip())
    return int(match.group(1)) * 60 + int(match.group(2)) if match else None


def afternoon_if_before_opening(tenant: dict, date: str, time_str: str) -> str:
    """'2:00' -> '14:00' when the business is shut at two in the morning and
    open at two in the afternoon; anything else is returned unchanged.

    A caller who says "tomorrow at two" means the afternoon. On a scripted
    call on 2026-11-24 the model passed "2:00" for it, was told two in the
    morning was taken, and offered the caller eight or nine instead. Only a
    time before that day's opening is moved, and only when twelve hours later
    falls inside the day's hours, so a real morning time is never changed. The
    agent says the time back ("two in the afternoon") before booking.
    """
    requested = _minutes(time_str)
    if requested is None or requested >= 12 * 60:
        return time_str
    try:
        weekday = _WEEKDAYS[datetime.strptime(date, "%Y-%m-%d").weekday()]
    except ValueError:
        return time_str
    hours = ((tenant or {}).get("working_hours") or {}).get(weekday) or {}
    opens, closes = _minutes(hours.get("open")), _minutes(hours.get("close"))
    if not hours.get("enabled") or opens is None or closes is None:
        return time_str
    shifted = requested + 12 * 60
    if requested < opens and opens <= shifted < closes:
        return f"{shifted // 60:02d}:{shifted % 60:02d}"
    return time_str


_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december"
    "|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
# Numbers that are part of a date, not a time of day: "the 7th", "December 7",
# "7 December", "2026".
_DATE_NUMBER_RE = re.compile(
    rf"\b\d{{1,2}}(?:st|nd|rd|th)\b|\b(?:{_MONTHS})\.?\s+\d{{1,2}}\b"
    rf"|\b\d{{1,2}}\s+(?:of\s+)?(?:{_MONTHS})\b|\b(?:19|20)\d{{2}}\b",
    re.IGNORECASE,
)
_SPOKEN_TIME_RE = re.compile(
    r"(?<!\d)\d{1,2}(?:[:.]\d{2})?(?!\d)"
    r"|\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|noon|midday|midnight)\b"
    r"|\bone\s+(?:o\W?clock|thirty|fifteen|forty|p\.?m|a\.?m)|\bat\s+one\b"
    r"|\bhalf\s+past\b|\bquarter\s+(?:past|to)\b|o['’ ]?clock",
    re.IGNORECASE,
)


def names_a_time(text: str) -> bool:
    """Whether something a caller said contains a time of day ("at two",
    "3:30", "ten o'clock"). A day, a date or "the afternoon" is not one."""
    return bool(_SPOKEN_TIME_RE.search(_DATE_NUMBER_RE.sub(" ", text or "")))


_WHAT_IS_FREE_RE = re.compile(
    r"\bwhat\s+(?:times?|slots?|do\s+you\s+have|have\s+you\s+got|is\s+(?:free|available|open))"
    r"|\bwhich\s+times?\b|\bwhen\s+(?:are|is|do|can)\s+you\b|\bavailab\w+|\bopenings?\b"
    r"|\b(?:earliest|soonest|next\s+free|any\s+time|anytime|whenever)\b",
    re.IGNORECASE,
)


def asked_what_is_free(deps: dict) -> bool:
    """The caller has asked which times are open (since the last booking
    change), so listing times answers them."""
    since = deps.get("_last_write_at", 0)
    return any(
        _WHAT_IS_FREE_RE.search(text or "")
        for at, text in (deps.get("_caller_texts") or []) if at > since
    )


def time_not_given(deps: dict, date: str) -> bool:
    """True, once per date, when this call must ask the caller for a time
    (``deps["_ask_for_time"]``, set by the demo agent) and the caller has not
    said one since the last booking change.

    The demo agent's prompt tells the model to ask "what time would you like?"
    when the caller names only a day. On a scripted call on 2026-11-25 it
    called check_slot with a time of its own choosing ("08:00") instead. The
    second check_slot for the same date goes through, so a time this test
    cannot read (another language, an unusual phrasing) costs one question,
    never the booking.
    """
    if not deps.get("_ask_for_time"):
        return False
    since = deps.get("_last_write_at", 0)
    said = [text for at, text in (deps.get("_caller_texts") or []) if at > since]
    if not said or any(names_a_time(text) for text in said):
        return False
    asked = deps.setdefault("_time_asked_dates", set())
    if date in asked:
        return False
    asked.add(date)
    return True


async def check_slot_impl(deps: dict, date: str, time_str: str) -> str:
    if not date or not time_str:
        return state(deps, "STATE:missing_args | DIRECTIVE:ask the caller for a day and a time.")
    if time_not_given(deps, date):
        lib.log_tool_call(deps, {"name": "check_slot", "success": True, "result": "time_not_given"})
        return state(
            deps,
            f"STATE:time_not_given date={date}"
            " | DIRECTIVE:nothing was checked: the caller has not said a time, so"
            " you may not choose one. Ask what time they would like on that day,"
            " in one short sentence, then call check_slot with their answer.",
        )
    ctx = await _context(deps)
    if isinstance(ctx, str):
        return ctx
    tenant, tz, sched = ctx

    today = lib.tenant_today(tz)
    if date < today:
        return _past_date(deps, date, today)
    time_str = afternoon_if_before_opening(tenant, date, time_str)
    requested = lib.parse_hhmm_to_utc(time_str, date, tz)
    if requested is None:
        return state(deps, "STATE:bad_time | DIRECTIVE:ask the caller to say the time again.")

    slots = open_slots(tenant, date, sched, tz)
    requested_speech = say_time(requested.isoformat(), tz)
    now = datetime.now(timezone.utc)

    if date == today and requested < now + MIN_NOTICE:
        if slots:
            speech, token = _offer(deps, slots[0], tz)
            deps["_last_offered_token"] = token
            lib.log_tool_call(deps, {"name": "check_slot", "success": True, "result": "too_soon", "slot_token": token})
            return state(
                deps,
                f"STATE:too_soon requested={requested_speech} earliest_today={speech} token={token}"
                " | DIRECTIVE:say that time is too soon (one hour's notice is needed) and"
                " offer the earliest time above or another day.",
            )
        later = next_opening(deps, tenant, sched, tz, date)
        if later:
            lib.log_tool_call(deps, {"name": "check_slot", "success": True, "result": "too_soon", "slot_token": later[1]})
            return state(
                deps,
                f"STATE:too_soon requested={requested_speech} nothing_left_today=true"
                f" next_open={later[0]} token={later[1]}"
                " | DIRECTIVE:say nothing is left today and offer the next opening above"
                " or another day.",
            )
        return state(
            deps,
            f"STATE:too_soon requested={requested_speech} nothing_left_today=true"
            " | DIRECTIVE:say nothing is left today and ask which other day works.",
        )

    match = next(
        (s for s in slots if _start_dt(s) <= requested < _start_dt(s) + _duration(s)),
        None,
    )
    if match:
        speech, token = _offer(deps, match, tz)
        deps["_last_offered_token"] = token
        lib.log_tool_call(deps, {"name": "check_slot", "success": True, "result": "available", "slot_token": token})
        return state(
            deps,
            f"STATE:slot_ok token={token} speech={speech}"
            " | DIRECTIVE:that time is free. If the caller has not yet agreed to this"
            " exact time, say it back and ask. Use this token to book or reschedule.",
        )

    if not slots:
        label = lib.format_date_label(date, tz)
        later = next_opening(deps, tenant, sched, tz, date)
        if later:
            return state(
                deps,
                f"STATE:day_empty date_label={label} next_open={later[0]} token={later[1]}"
                " | DIRECTIVE:say nothing is open that day and offer the next opening"
                " above or another day.",
            )
        return state(
            deps,
            f"STATE:day_empty date_label={label}"
            " | DIRECTIVE:say nothing is open that day and ask which other day works.",
        )

    # Taken: the closest open times that day.
    deps.pop("_last_offered_token", None)
    nearest = sorted(slots, key=lambda s: abs((_start_dt(s) - requested).total_seconds()))[:3]
    nearest.sort(key=lambda s: s["start"])
    parts = []
    for i, slot in enumerate(nearest, 1):
        speech, token = _offer(deps, slot, tz)
        parts.append(f"{i}.{speech} token={token}")
    lib.log_tool_call(deps, {"name": "check_slot", "success": True, "result": "taken"})
    return state(
        deps,
        f"STATE:slot_taken requested={requested_speech} alts={len(nearest)}"
        f" | ALTS: {'; '.join(parts)}"
        " | DIRECTIVE:say that time is not free and offer one or two of these"
        " instead; use the chosen one's token.",
    )


def _duration(slot: dict) -> timedelta:
    end = slot["end"][:-1] + "+00:00" if slot["end"].endswith("Z") else slot["end"]
    return datetime.fromisoformat(end) - _start_dt(slot)


# ── check_day ──────────────────────────────────────────────────────────────

async def check_day_impl(deps: dict, date: str, part_of_day: str = "") -> str:
    # Asking about a day supersedes any single time offered before it.
    deps.pop("_last_offered_token", None)
    if not date:
        return state(deps, "STATE:missing_args | DIRECTIVE:ask the caller which day.")
    if not asked_what_is_free(deps) and time_not_given(deps, date):
        # The demo agent does not offer times of its own choosing: a caller
        # who named a day and no time is asked what time they would like.
        lib.log_tool_call(deps, {"name": "check_day", "success": True, "result": "time_not_given"})
        return state(
            deps,
            f"STATE:time_not_given date={date}"
            " | DIRECTIVE:nothing was looked up: do not suggest times. Ask what"
            " time the caller would like on that day, in one short sentence,"
            " then call check_slot with their answer.",
        )
    ctx = await _context(deps)
    if isinstance(ctx, str):
        return ctx
    tenant, tz, sched = ctx
    if date < lib.tenant_today(tz):
        return _past_date(deps, date, lib.tenant_today(tz))

    slots = open_slots(tenant, date, sched, tz)
    label = lib.format_date_label(date, tz)
    if not slots:
        later = next_opening(deps, tenant, sched, tz, date)
        lib.log_tool_call(deps, {"name": "check_day", "success": True, "result": "empty", "date": date})
        if later:
            return state(
                deps,
                f"STATE:day_empty date_label={label} next_open={later[0]} token={later[1]}"
                " | DIRECTIVE:say nothing is open that day and offer the next opening"
                " above or another day.",
            )
        return state(
            deps,
            f"STATE:day_empty date_label={label}"
            " | DIRECTIVE:say nothing is open that day and ask which other day works.",
        )

    wanted = _PARTS_OF_DAY.get((part_of_day or "").strip().lower())
    pool, note = slots, ""
    if wanted:
        in_part = [s for s in slots if wanted[0] <= local_hour(s["start"], tz) < wanted[1]]
        if wanted is _PARTS_OF_DAY["afternoon"]:
            # Twelve o'clock is "noon" to a caller, not "the afternoon":
            # offer it only when the afternoon has nothing later.
            in_part = [s for s in in_part if local_hour(s["start"], tz) >= 13] or in_part
        if in_part:
            pool = in_part
        else:
            note = f" none_in={part_of_day}"
    # Exactly two options, in the order they will be spoken, so "the first
    # one" means one thing. With three returned and two spoken, a call on
    # 2026-11-24 booked the option the caller had never heard.
    parts, tokens = [], []
    for i, slot in enumerate(lib.pick_spread(pool, OPTIONS_PER_DAY), 1):
        speech, token = _offer(deps, slot, tz)
        tokens.append(token)
        parts.append(f"{i}.{speech} token={token}")
    lib.log_tool_call(deps, {
        "name": "check_day", "success": True, "result": "has_slots", "date": date, "slot_tokens": tokens,
    })
    directive = (
        "offer exactly these times, in this order, in one natural sentence."
        " \"The first one\" then means option 1. If the caller names a"
        " different time, verify it with check_slot."
    )
    if note:
        directive = f"say nothing is open in the {part_of_day} that day, then " + directive
    return state(
        deps,
        f"STATE:day_has_slots date_label={label} count={len(pool)}{note}"
        f" | OPTIONS: {'; '.join(parts)} | DIRECTIVE:{directive}",
    )


# ── next_available_days ────────────────────────────────────────────────────

async def next_available_days_impl(deps: dict) -> str:
    ctx = await _context(deps)
    if isinstance(ctx, str):
        return ctx
    tenant, tz, sched = ctx
    days = []
    for date in lib.next_n_local_dates(LOOKAHEAD_DAYS, tz):
        if open_slots(tenant, date, sched, tz):
            days.append(f"{lib.format_date_label(date, tz)} date={date}")
        if len(days) == 3:
            break
    lib.log_tool_call(deps, {"name": "next_available_days", "success": True, "open_days": len(days)})
    if days:
        return state(
            deps,
            f"STATE:has_near_availability days={'; '.join(days)}"
            " | DIRECTIVE:offer these days in a natural sentence, then call check_day"
            " for the one the caller picks.",
        )
    return state(
        deps,
        "STATE:no_near_availability"
        " | DIRECTIVE:say the next few days are full and ask if a later date would work.",
    )


# ── tool factories ─────────────────────────────────────────────────────────

async def _run(deps: dict, context, name: str, work) -> str:
    try:
        async with tool_filler(context, deps, "generic", delay=LOOKUP_FILLER_DELAY_S):
            result = await work()
        logger.info("[appointments] %s -> %s", name, (result or "")[:180])
        return result
    except Exception as err:  # noqa: BLE001
        logger.error("[appointments] %s failed: %r", name, err)
        return state(deps, f"STATE:lookup_failed | DIRECTIVE:{TRY_AGAIN}.")


def create_check_slot_tool(deps: dict):
    @function_tool(raw_schema=_CHECK_SLOT)
    async def check_slot(raw_arguments: dict, context: RunContext) -> str:
        date = (raw_arguments.get("date") or "").strip()
        time_str = (raw_arguments.get("time") or "").strip()
        return await _run(deps, context, "check_slot", lambda: check_slot_impl(deps, date, time_str))

    return check_slot


def create_check_day_tool(deps: dict):
    @function_tool(raw_schema=_CHECK_DAY)
    async def check_day(raw_arguments: dict, context: RunContext) -> str:
        date = (raw_arguments.get("date") or "").strip()
        part = (raw_arguments.get("part_of_day") or "").strip()
        return await _run(deps, context, "check_day", lambda: check_day_impl(deps, date, part))

    return check_day


def create_next_available_days_tool(deps: dict):
    @function_tool(raw_schema=_NEXT_DAYS)
    async def next_available_days(raw_arguments: dict, context: RunContext) -> str:
        return await _run(deps, context, "next_available_days", lambda: next_available_days_impl(deps))

    return next_available_days
