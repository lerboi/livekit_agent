"""Appointments agent (src/agents/appointments, src/calendars).

Covers: which agent a call gets, which calendar a call may write to, the
prompt and tool surface, availability lookups, booking, finding, moving and
cancelling (including the confirm gates and the test-call scope), the claim
guard, and the calendar clients' requests. Tools are exercised through their
``*_impl`` functions with the database and calendar layers replaced.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.agents import APPOINTMENTS, RECEPTIONIST, select_agent
from src.agents.appointments import calendar_sync, guard, store
from src.agents.appointments.prompt import build_prompt, date_context
from src.agents.appointments.tools import TOOL_NAMES, create_tools
from src.agents.appointments.tools import availability, book, manage
from src.agents.appointments.tools.common import register_appointment, say_time
from src.calendars import calendar_target, open_calendar
from src.calendars import google as google_cal
from src.calendars.google import CalendarError, GoogleCalendar
from src.calendars.outlook import OutlookCalendar

_SRC = Path(__file__).parent.parent / "src"
TZ = "Asia/Singapore"
HOURS = {
    day: {"enabled": True, "open": "08:00", "close": "17:00"}
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}


def _tool_name(tool) -> str:
    info = getattr(tool, "info", None)
    name = getattr(info, "name", None) or getattr(tool, "name", None)
    if name:
        return name
    raw = getattr(info, "raw_schema", None) or {}
    return raw.get("name") or ""


def _day(offset: int) -> str:
    return (datetime.now(ZoneInfo(TZ)) + timedelta(days=offset)).strftime("%Y-%m-%d")


def _utc(date: str, hhmm: str) -> str:
    hour, minute = (int(x) for x in hhmm.split(":"))
    year, month, day = (int(x) for x in date.split("-"))
    local = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(TZ))
    return local.astimezone(timezone.utc).isoformat()


def _deps(**overrides) -> dict:
    deps = {
        "supabase": None,
        "tenant_id": "tenant-1",
        "tenant": {
            "id": "tenant-1",
            "business_name": "Voco",
            "tenant_timezone": TZ,
            "working_hours": HOURS,
            "slot_duration_mins": 60,
            "travel_buffer_mins": 30,
        },
        "tenant_timezone": TZ,
        "call_id": "test-web-tenant-1-1",
        "call_uuid": "call-uuid-1",
        "from_number": "",
        "is_test_call": True,
        "demo": None,
        "call_end_reason": ["caller_hangup"],
        "_tool_call_log": [],
        "_last_user_turn_at": None,
        "_calendar": None,
        "_calendar_task": None,
        "_calendar_provider": None,
        "_slot_cache": {
            "fetched_at": time.time(),
            "appointments": [],
            "calendar_events": [],
            "service_zones": [],
            "zone_travel_buffers": [],
            "calendar_blocks": [],
        },
    }
    deps.update(overrides)
    return deps


def _token_in(state: str) -> str:
    return state.split("token=")[1].split()[0].rstrip(";")


# ── which agent takes a call ────────────────────────────────────────────────


def test_default_agent_is_the_receptionist(monkeypatch):
    monkeypatch.delenv("VOCO_DEFAULT_AGENT", raising=False)
    assert select_agent({}, False) == RECEPTIONIST
    assert select_agent(None, True) == RECEPTIONIST


def test_test_call_metadata_selects_the_appointments_agent(monkeypatch):
    monkeypatch.delenv("VOCO_DEFAULT_AGENT", raising=False)
    assert select_agent({"agent": "appointments"}, True) == APPOINTMENTS
    assert select_agent({"agent": " Appointments "}, True) == APPOINTMENTS


def test_room_metadata_cannot_change_the_agent_on_a_production_call(monkeypatch):
    monkeypatch.delenv("VOCO_DEFAULT_AGENT", raising=False)
    assert select_agent({"agent": "appointments"}, False) == RECEPTIONIST


def test_unknown_agent_names_fall_back(monkeypatch):
    monkeypatch.delenv("VOCO_DEFAULT_AGENT", raising=False)
    assert select_agent({"agent": "something-else"}, True) == RECEPTIONIST
    monkeypatch.setenv("VOCO_DEFAULT_AGENT", "nonsense")
    assert select_agent({}, False) == RECEPTIONIST


def test_env_default_moves_every_call(monkeypatch):
    monkeypatch.setenv("VOCO_DEFAULT_AGENT", "appointments")
    assert select_agent({}, False) == APPOINTMENTS
    assert select_agent({"agent": "receptionist"}, True) == RECEPTIONIST


def test_router_sits_before_the_receptionist_tenant_lookup():
    src = (_SRC / "agent.py").read_text(encoding="utf-8")
    route = src.index("if select_agent(room_meta, is_test_call) == APPOINTMENTS:")
    assert src.index("demo = parse_demo(room_meta, is_test_call)") < route
    assert route < src.index("# ── Tenant lookup")
    block = src[route:src.index("# ── Tenant lookup")]
    assert "sys.modules[__name__]" in block and "return" in block


# ── which calendar a call may write to ──────────────────────────────────────


def test_production_calls_write_to_the_primary_calendar():
    assert calendar_target(False, None) == (True, None)
    # A demo block on a production call is not honoured anywhere.
    assert calendar_target(False, {"destination": "outlook"}) == (True, None)


def test_plain_test_calls_never_write_to_a_calendar():
    assert calendar_target(True, None) == (False, None)
    assert calendar_target(True, {"destination": "voco"}) == (False, None)


def test_demo_calls_write_only_to_the_named_calendar():
    assert calendar_target(True, {"destination": "google"}) == (True, "google")
    assert calendar_target(True, {"destination": "outlook"}) == (True, "outlook")


class _Query:
    """Records a PostgREST-style chain and returns preset rows."""

    def __init__(self, db, table):
        self.db, self.table, self.ops, self.payload = db, table, [], None

    def __getattr__(self, name):
        def method(*args, **kwargs):
            if name in ("update", "upsert", "delete", "insert"):
                self.payload = args[0] if args else None
            self.ops.append((name, args))
            return self
        return method

    def execute(self):
        self.db.calls.append(self)
        handler = self.db.handlers.get(self.table)
        data = handler(self) if callable(handler) else (handler or [])
        return SimpleNamespace(data=data)


class _DB:
    def __init__(self, **handlers):
        self.handlers, self.calls = handlers, []

    def table(self, name):
        return _Query(self, name)

    def writes(self, table):
        return [q for q in self.calls if q.table == table and q.payload is not None or
                (q.table == table and any(op == "delete" for op, _ in q.ops))]


def test_open_calendar_prefers_the_primary_connection():
    db = _DB(calendar_credentials=[
        {"provider": "outlook", "access_token": "a", "is_primary": False},
        {"provider": "google", "access_token": "b", "is_primary": True},
    ])
    assert isinstance(open_calendar(db, "tenant-1"), GoogleCalendar)


def test_open_calendar_with_a_named_provider_never_falls_back():
    db = _DB(calendar_credentials=[{"provider": "google", "access_token": "b", "is_primary": True}])
    assert open_calendar(db, "tenant-1", "outlook") is None
    assert isinstance(open_calendar(db, "tenant-1", "google"), GoogleCalendar)
    assert open_calendar(db, "tenant-1", "icloud") is None
    assert open_calendar(_DB(calendar_credentials=[]), "tenant-1") is None


# ── calendar clients ────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body


def _fresh_creds(**extra):
    return {
        "access_token": "tok",
        "refresh_token": "refresh",
        "expiry_date": int(time.time() * 1000) + 3_600_000,
        "calendar_id": "primary",
        **extra,
    }


def test_google_create_sends_local_time_with_timezone(monkeypatch):
    sent = {}

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        sent.update(method=method, url=url, headers=headers, json=kwargs.get("json"))
        return _Resp(200, {"id": "evt1", "htmlLink": "https://calendar/evt1"})

    monkeypatch.setattr(google_cal.requests, "request", fake_request)
    cal = GoogleCalendar("tenant-1", _fresh_creds(), _DB())
    out = cal.create_event(
        title="Alex Tan: Product demo", body="Booked by the Voco voice agent",
        start_utc="2026-11-25T04:00:00+00:00", end_utc="2026-11-25T05:00:00+00:00", tz=TZ,
    )
    assert out == {"provider": "google", "event_id": "evt1", "html_link": "https://calendar/evt1"}
    assert sent["method"] == "POST" and sent["url"].endswith("/calendars/primary/events")
    assert sent["headers"]["Authorization"] == "Bearer tok"
    assert sent["json"]["summary"] == "Alex Tan: Product demo"
    assert sent["json"]["start"] == {"dateTime": "2026-11-25T12:00:00", "timeZone": TZ}
    assert sent["json"]["end"] == {"dateTime": "2026-11-25T13:00:00", "timeZone": TZ}


def test_google_move_patches_only_the_times(monkeypatch):
    sent = {}

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        sent.update(method=method, url=url, json=kwargs.get("json"))
        return _Resp(200, {"id": "evt1", "htmlLink": "link"})

    monkeypatch.setattr(google_cal.requests, "request", fake_request)
    cal = GoogleCalendar("tenant-1", _fresh_creds(), _DB())
    cal.move_event("evt1", start_utc="2026-11-26T02:00:00+00:00", end_utc="2026-11-26T03:00:00+00:00", tz=TZ)
    assert sent["method"] == "PATCH" and sent["url"].endswith("/events/evt1")
    assert set(sent["json"]) == {"start", "end"}
    assert sent["json"]["start"]["dateTime"] == "2026-11-26T10:00:00"


def test_google_delete_treats_an_already_removed_event_as_done(monkeypatch):
    monkeypatch.setattr(google_cal.requests, "request", lambda *a, **k: _Resp(410))
    GoogleCalendar("tenant-1", _fresh_creds(), _DB()).delete_event("evt1")
    monkeypatch.setattr(google_cal.requests, "request", lambda *a, **k: _Resp(500))
    with pytest.raises(CalendarError):
        GoogleCalendar("tenant-1", _fresh_creds(), _DB()).delete_event("evt1")


def test_google_refreshes_an_expired_token_and_stores_it(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    posted = {}

    def fake_post(url, data=None, timeout=None):
        posted.update(url=url, data=data)
        return _Resp(200, {"access_token": "new-tok", "expires_in": 3600})

    used = []

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        used.append(headers["Authorization"])
        return _Resp(200, {"id": "evt1"})

    monkeypatch.setattr(google_cal.requests, "post", fake_post)
    monkeypatch.setattr(google_cal.requests, "request", fake_request)
    db = _DB()
    cal = GoogleCalendar("tenant-1", _fresh_creds(expiry_date=1), db)
    cal.prepare()
    cal.create_event(title="t", body="b", start_utc="2026-11-25T04:00:00+00:00",
                     end_utc="2026-11-25T05:00:00+00:00", tz=TZ)
    assert posted["data"]["grant_type"] == "refresh_token"
    assert posted["data"]["refresh_token"] == "refresh"
    assert used == ["Bearer new-tok"]
    stored = [q for q in db.calls if q.table == "calendar_credentials"]
    assert stored and stored[0].payload["access_token"] == "new-tok"
    assert stored[0].payload["expiry_date"] > time.time() * 1000


def test_google_retries_once_after_a_401(monkeypatch):
    monkeypatch.setattr(google_cal.requests, "post",
                        lambda *a, **k: _Resp(200, {"access_token": "new-tok", "expires_in": 3600}))
    answers = [_Resp(401), _Resp(200, {"id": "evt1"})]
    monkeypatch.setattr(google_cal.requests, "request", lambda *a, **k: answers.pop(0))
    cal = GoogleCalendar("tenant-1", _fresh_creds(), _DB())
    assert cal.create_event(title="t", body="b", start_utc="2026-11-25T04:00:00+00:00",
                            end_utc="2026-11-25T05:00:00+00:00", tz=TZ)["event_id"] == "evt1"
    assert answers == []


def test_outlook_create_uses_graph_fields(monkeypatch):
    from src.calendars import outlook as outlook_cal

    sent = {}

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        sent.update(method=method, url=url, json=kwargs.get("json"))
        return _Resp(201, {"id": "o1", "webLink": "https://outlook/o1"})

    monkeypatch.setattr(outlook_cal.requests, "request", fake_request)
    cal = OutlookCalendar("tenant-1", _fresh_creds(), _DB())
    out = cal.create_event(title="Alex Tan: Demo", body="Booked by the Voco voice agent",
                           start_utc="2026-11-25T04:00:00+00:00", end_utc="2026-11-25T05:00:00+00:00", tz=TZ)
    assert out == {"provider": "outlook", "event_id": "o1", "html_link": "https://outlook/o1"}
    assert sent["url"].endswith("/me/events")
    assert sent["json"]["subject"] == "Alex Tan: Demo"
    assert sent["json"]["start"] == {"dateTime": "2026-11-25T12:00:00", "timeZone": TZ}


# ── prompt and tool surface ─────────────────────────────────────────────────


def test_tool_surface_is_the_eight_appointment_tools():
    names = [_tool_name(t) for t in create_tools(_deps())]
    assert tuple(names) == TOOL_NAMES
    for absent in ("capture_lead", "transfer_call", "validate_address", "check_caller_history"):
        assert absent not in names


def test_prompt_states_the_job_and_the_rules():
    prompt = build_prompt(business_name="Acme Dental", working_hours=HOURS, tenant_timezone=TZ)
    assert "You are the voice assistant for Acme Dental." in prompt
    for tool in TOOL_NAMES:
        assert tool in prompt
    assert "Only after it returns BOOKED may you say it is booked." in prompt
    assert "Only after it returns RESCHEDULED" in prompt
    assert "Only after it returns CANCELLED" in prompt
    assert "Never invent one" in prompt
    assert "BUSINESS HOURS" in prompt


def test_prompt_has_no_home_service_content():
    prompt = build_prompt(business_name="Acme Dental", working_hours=HOURS, tenant_timezone=TZ).lower()
    for word in ("address", "emergency", "urgent", "capture_lead", "transfer", "technician", "plumb"):
        assert word not in prompt
    for model in ("groq", "openai"):
        text = build_prompt(business_name="Acme Dental", model=model).lower()
        assert "capture_lead" not in text and "address" not in text


def test_prompt_is_stable_and_takes_any_reason():
    a = build_prompt(business_name="Voco", working_hours=HOURS, tenant_timezone=TZ)
    b = build_prompt(business_name="Voco", working_hours=HOURS, tenant_timezone=TZ)
    assert a == b and "Today is" not in a
    assert "never question or turn down the reason" in a


def test_date_context_gives_the_model_every_date_it_needs():
    text = date_context(datetime(2026, 11, 24, 9, 30))
    assert text.startswith("Today is Tuesday, November 24, 2026 (2026-11-24).")
    assert "- tomorrow, Wednesday November 25: 2026-11-25" in text
    assert "- Thursday November 26: 2026-11-26" in text
    assert "- Tuesday December 8: 2026-12-08" in text  # two weeks out, across the month end
    assert len([line for line in text.splitlines() if line.startswith("- ")]) == 14


async def test_a_past_date_is_sent_back_for_a_silent_retry():
    out = await availability.check_day_impl(_deps(), _day(-30), "afternoon")
    assert out.startswith("STATE:past_date") and f"today={_day(0)}" in out
    assert "Do not mention this to the caller" in out and "call the tool again" in out


def test_the_tenants_home_service_templates_are_not_loaded():
    # The demo tenant's `services` rows are plumbing templates; in the prompt
    # they made the agent refuse a product-demo booking on a live call.
    import inspect

    entry = (_SRC / "agents" / "appointments" / "entry.py").read_text(encoding="utf-8")
    assert "services(" not in entry and "intake_questions" not in entry
    assert "services" not in inspect.signature(build_prompt).parameters


def test_no_tool_return_mentions_tools_this_agent_lacks():
    for name in ("availability.py", "book.py", "manage.py", "common.py"):
        text = (_SRC / "agents" / "appointments" / "tools" / name).read_text(encoding="utf-8")
        for absent in ("capture_lead", "transfer_call", "validate_address"):
            body = text.split('"""', 2)[2]  # the module docstring may name them
            assert absent not in body, f"{name} mentions {absent}"


def test_noon_is_labelled():
    assert say_time(_utc("2026-11-25", "12:00"), TZ).endswith("12:00 PM (noon)")
    assert "(noon)" not in say_time(_utc("2026-11-25", "14:00"), TZ)


# ── availability ────────────────────────────────────────────────────────────


async def test_check_slot_free_time_returns_a_token():
    deps = _deps()
    out = await availability.check_slot_impl(deps, _day(2), "14:00")
    assert out.startswith("STATE:slot_ok token=slot_")
    assert _token_in(out) in deps["_slot_tokens"]
    assert deps["_slot_tokens"][_token_in(out)]["slot_start_utc"].startswith(_utc(_day(2), "14:00")[:16])


async def test_check_slot_taken_time_offers_alternatives_that_day():
    date = _day(2)
    deps = _deps()
    deps["_slot_cache"]["appointments"] = [
        {"start_time": _utc(date, "14:00"), "end_time": _utc(date, "15:00"), "zone_id": None},
    ]
    out = await availability.check_slot_impl(deps, date, "14:00")
    assert out.startswith("STATE:slot_taken")
    assert "ALTS:" in out and "token=slot_" in out


async def test_no_travel_time_is_kept_between_appointments():
    date = _day(2)
    deps = _deps()
    deps["_slot_cache"]["appointments"] = [
        {"start_time": _utc(date, "14:00"), "end_time": _utc(date, "15:00"), "zone_id": None},
    ]
    # The receptionist keeps 30 minutes clear either side of a booking; an
    # appointment here can start the moment the previous one ends.
    assert (await availability.check_slot_impl(deps, date, "15:00")).startswith("STATE:slot_ok")
    assert (await availability.check_slot_impl(deps, date, "13:00")).startswith("STATE:slot_ok")


async def test_check_slot_rejects_past_dates_and_bad_input():
    deps = _deps()
    assert (await availability.check_slot_impl(deps, _day(-1), "10:00")).startswith("STATE:past_date")
    assert (await availability.check_slot_impl(deps, "", "")).startswith("STATE:missing_args")
    assert (await availability.check_slot_impl(_deps(tenant_id=None), _day(1), "10:00")).startswith(
        "STATE:lookup_failed"
    )


async def test_check_day_offers_exactly_two_times_in_order():
    deps = _deps()
    out = await availability.check_day_impl(deps, _day(2))
    assert out.startswith("STATE:day_has_slots") and "count=9" in out
    assert out.count("token=slot_") == 2
    options = out.split("OPTIONS:")[1].split("| DIRECTIVE")[0]
    assert options.index("1.") < options.index("2.") and "8:00 AM" in options and "4:00 PM" in options
    assert "exactly these times, in this order" in out


async def test_check_day_filters_by_part_of_day():
    deps = _deps()
    out = await availability.check_day_impl(deps, _day(2), "afternoon")
    options = out.split("OPTIONS:")[1].split("| DIRECTIVE")[0]
    assert " AM" not in options and "count=4" in out
    assert "12:00 PM" not in options  # noon is not "the afternoon" while later times are open
    morning = await availability.check_day_impl(deps, _day(2), "morning")
    assert " PM" not in morning.split("OPTIONS:")[1].split("| DIRECTIVE")[0]
    evening = await availability.check_day_impl(deps, _day(2), "evening")
    assert "none_in=evening" in evening and "token=slot_" in evening


async def test_check_day_full_day_points_at_the_next_opening():
    date = _day(2)
    deps = _deps()
    deps["_slot_cache"]["calendar_blocks"] = [
        {"start_time": _utc(date, "00:00"), "end_time": _utc(date, "23:59"), "is_all_day": False},
    ]
    out = await availability.check_day_impl(deps, date)
    assert out.startswith("STATE:day_empty") and "next_open=" in out and "token=slot_" in out


async def test_next_available_days_lists_days_with_dates():
    out = await availability.next_available_days_impl(_deps())
    assert out.startswith("STATE:has_near_availability")
    assert out.count("date=") == 3


# ── booking ─────────────────────────────────────────────────────────────────


@pytest.fixture
def writes(monkeypatch):
    """Replace the database and calendar layers; record what was asked."""
    log = SimpleNamespace(booked=[], details=[], created=[], moved=[], removed=[],
                          moves=[], cancels=[], book_result={"success": True, "appointment_id": "appt-1"},
                          move_result="moved", cancel_result=True, upcoming=[])

    async def fake_book(deps, *, start_utc, end_utc, caller_name):
        log.booked.append((start_utc, end_utc, caller_name))
        return log.book_result

    async def fake_details(deps, appointment_id, *, reason):
        log.details.append((appointment_id, reason))

    async def fake_move(deps, appointment, *, start_utc, end_utc):
        log.moves.append((appointment["id"], start_utc, end_utc))
        return log.move_result

    async def fake_cancel(deps, appointment):
        log.cancels.append(appointment["id"])
        return log.cancel_result

    async def fake_upcoming(deps):
        return [dict(row) for row in log.upcoming]

    async def cal_create(deps, appointment):
        log.created.append(dict(appointment))
        return calendar_sync.SAVED

    async def cal_move(deps, appointment):
        log.moved.append(dict(appointment))
        return calendar_sync.SAVED

    async def cal_remove(deps, appointment):
        log.removed.append(dict(appointment))
        return calendar_sync.SAVED

    monkeypatch.setattr(store, "book", fake_book)
    monkeypatch.setattr(store, "save_details", fake_details)
    monkeypatch.setattr(store, "move", fake_move)
    monkeypatch.setattr(store, "cancel", fake_cancel)
    monkeypatch.setattr(store, "upcoming", fake_upcoming)
    monkeypatch.setattr(calendar_sync, "create", cal_create)
    monkeypatch.setattr(calendar_sync, "move", cal_move)
    monkeypatch.setattr(calendar_sync, "remove", cal_remove)
    return log


async def _offer(deps, date=None, hhmm="14:00") -> str:
    out = await availability.check_slot_impl(deps, date or _day(2), hhmm)
    assert out.startswith("STATE:slot_ok"), out
    return _token_in(out)


async def test_booking_takes_the_slot_then_writes_the_calendar(writes):
    deps = _deps()
    token = await _offer(deps)
    out = await book.book_appointment_impl(
        deps, {"slot_token": token, "caller_name": "Alex Tan", "reason": "product demo"},
    )
    assert out.startswith("BOOKED:")
    assert writes.booked == [(deps["_slot_tokens"][token]["slot_start_utc"],
                              deps["_slot_tokens"][token]["slot_end_utc"], "Alex Tan")]
    assert writes.details == [("appt-1", "product demo")]
    assert writes.created[0]["id"] == "appt-1" and writes.created[0]["notes"] == "product demo"
    assert deps["_booking_succeeded"] is True and deps["_booked_appointment_id"] == "appt-1"
    assert "_slot_cache" not in deps
    entry = deps["_tool_call_log"][-1]
    assert entry["name"] == "book_appointment" and entry["success"] and entry["appointment_id"] == "appt-1"


async def test_booking_never_accepts_a_made_up_token(writes):
    deps = _deps()
    out = await book.book_appointment_impl(deps, {"slot_token": "slot_deadbeef", "caller_name": "Alex"})
    assert out.startswith("STATE:booking_invalid")
    assert writes.booked == []


async def test_booking_the_same_slot_twice_books_once(writes):
    deps = _deps()
    token = await _offer(deps)
    first = await book.book_appointment_impl(deps, {"slot_token": token, "caller_name": "Alex"})
    second = await book.book_appointment_impl(deps, {"slot_token": token, "caller_name": "Alex"})
    assert first == second and len(writes.booked) == 1


async def test_a_taken_slot_books_nothing_and_offers_the_next_time(writes, monkeypatch):
    deps = _deps()
    token = await _offer(deps)
    writes.book_result = {"success": False}

    async def refetch(d):  # the fresh look-up after the cache was dropped
        return {"appointments": [], "calendar_events": [], "service_zones": [],
                "zone_travel_buffers": [], "calendar_blocks": []}

    monkeypatch.setattr(availability.lib, "fetch_scheduling_data", refetch)
    out = await book.book_appointment_impl(deps, {"slot_token": token, "caller_name": "Alex"})
    assert out.startswith("STATE:slot_taken next_available=")
    assert "nothing is booked" in out
    assert writes.created == [] and not deps.get("_booking_succeeded")


async def test_a_time_first_offered_in_this_turn_must_be_confirmed(writes):
    deps = _deps(_last_user_turn_at=time.time() - 5)
    token = await _offer(deps)  # offered after the caller last spoke
    out = await book.book_appointment_impl(deps, {"slot_token": token, "caller_name": "Alex"})
    assert out.startswith("STATE:needs_confirmation") and writes.booked == []
    deps["_last_user_turn_at"] = time.time() + 1  # the caller said yes
    assert (await book.book_appointment_impl(deps, {"slot_token": token, "caller_name": "Alex"})).startswith("BOOKED:")


async def test_a_booking_error_is_reported_as_not_booked(monkeypatch, writes):
    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "book", boom)
    deps = _deps()
    token = await _offer(deps)
    out = await book.book_appointment_impl(deps, {"slot_token": token, "caller_name": "Alex"})
    assert out.startswith("STATE:booking_failed") and "nothing is booked" in out


# ── find, move, cancel ──────────────────────────────────────────────────────


def _row(appt_id="appt-9", name="Alex Tan", date=None, hhmm="16:00", **extra):
    date = date or _day(1)
    return {
        "id": appt_id, "caller_name": name, "caller_phone": "", "notes": "product demo",
        "status": "confirmed", "call_id": "call-old",
        "start_time": _utc(date, hhmm),
        "end_time": _utc(date, f"{int(hhmm[:2]) + 1:02d}:00"),
        "external_event_id": None, "external_event_provider": None, **extra,
    }


def test_name_matching_tolerates_how_speech_to_text_spells_names():
    assert manage.name_matches("Alex Tann", "Alex Tan")
    assert manage.name_matches("alex", "Alex Tan")
    assert manage.name_matches("Alex Tan.", "alex tan")
    assert not manage.name_matches("Priya Shah", "Alex Tan")
    assert not manage.name_matches("", "Alex Tan")
    assert not manage.name_matches("Alex", "Caller")


async def test_find_returns_the_callers_appointment_with_a_token(writes):
    writes.upcoming = [_row(), _row("appt-other", "Priya Shah")]
    deps = _deps()
    out = await manage.find_appointment_impl(deps, "Alex Tan")
    assert out.startswith("STATE:appointment_found count=1")
    assert "token=appt_" in out and "Priya" not in out
    assert [e["appointment"]["id"] for e in deps["_appt_tokens"].values()] == ["appt-9"]


async def test_find_reports_nothing_found(writes):
    writes.upcoming = [_row("appt-other", "Priya Shah")]
    out = await manage.find_appointment_impl(_deps(), "Alex Tan")
    assert out.startswith("STATE:appointment_not_found")


async def test_find_on_a_production_call_is_scoped_to_the_phone_not_the_name(writes):
    # store.upcoming already returned only this phone's appointments.
    writes.upcoming = [_row(name="A. Tan")]
    out = await manage.find_appointment_impl(_deps(is_test_call=False, from_number="+6591234567"), "Alexander")
    assert out.startswith("STATE:appointment_found count=1")


async def test_reschedule_moves_the_row_then_the_calendar_event(writes):
    writes.upcoming = [_row(external_event_id="evt1", external_event_provider="google")]
    deps = _deps()
    found = await manage.find_appointment_impl(deps, "Alex Tan")
    slot = await _offer(deps, _day(3), "10:00")
    out = await manage.reschedule_appointment_impl(
        deps, {"appointment_token": _token_in(found), "slot_token": slot},
    )
    assert out.startswith("RESCHEDULED:")
    assert writes.moves == [("appt-9", deps["_slot_tokens"][slot]["slot_start_utc"],
                             deps["_slot_tokens"][slot]["slot_end_utc"])]
    assert writes.moved[0]["external_event_id"] == "evt1"
    assert writes.moved[0]["start_time"] == deps["_slot_tokens"][slot]["slot_start_utc"]
    assert writes.booked == []  # a move is never a new booking
    assert deps["_tool_call_log"][-1]["name"] == "reschedule_appointment"


async def test_reschedule_to_a_taken_time_changes_nothing(writes):
    writes.upcoming = [_row()]
    writes.move_result = "taken"
    deps = _deps()
    found = await manage.find_appointment_impl(deps, "Alex Tan")
    slot = await _offer(deps, _day(3), "10:00")
    out = await manage.reschedule_appointment_impl(
        deps, {"appointment_token": _token_in(found), "slot_token": slot},
    )
    assert out.startswith("STATE:slot_taken") and "nothing has changed" in out
    assert writes.moved == []


async def test_reschedule_needs_real_tokens(writes):
    deps = _deps()
    out = await manage.reschedule_appointment_impl(deps, {"appointment_token": "appt_x", "slot_token": "slot_x"})
    assert out.startswith("STATE:reschedule_invalid reason=unknown_appointment")
    writes.upcoming = [_row()]
    found = await manage.find_appointment_impl(deps, "Alex Tan")
    out = await manage.reschedule_appointment_impl(
        deps, {"appointment_token": _token_in(found), "slot_token": "slot_made_up"},
    )
    assert out.startswith("STATE:reschedule_invalid reason=unknown_slot")
    assert writes.moves == []


async def test_reschedule_to_a_time_offered_this_turn_must_be_confirmed(writes):
    writes.upcoming = [_row()]
    deps = _deps()
    found = await manage.find_appointment_impl(deps, "Alex Tan")
    deps["_last_user_turn_at"] = time.time()
    await asyncio.sleep(0.01)
    slot = await _offer(deps, _day(3), "10:00")
    args = {"appointment_token": _token_in(found), "slot_token": slot}
    assert (await manage.reschedule_appointment_impl(deps, args)).startswith("STATE:needs_confirmation")
    assert writes.moves == []
    deps["_last_user_turn_at"] = time.time() + 1
    assert (await manage.reschedule_appointment_impl(deps, args)).startswith("RESCHEDULED:")


async def test_cancel_cancels_the_row_and_removes_the_event(writes):
    writes.upcoming = [_row(external_event_id="evt1", external_event_provider="google")]
    deps = _deps()
    found = await manage.find_appointment_impl(deps, "Alex Tan")
    out = await manage.cancel_appointment_impl(deps, {"appointment_token": _token_in(found)})
    assert out.startswith("CANCELLED:")
    assert writes.cancels == ["appt-9"] and writes.removed[0]["external_event_id"] == "evt1"
    assert deps["_appt_tokens"] == {}


async def test_cancel_in_the_same_turn_as_the_lookup_must_be_confirmed(writes):
    writes.upcoming = [_row()]
    deps = _deps(_last_user_turn_at=time.time() - 5)
    found = await manage.find_appointment_impl(deps, "Alex Tan")
    args = {"appointment_token": _token_in(found)}
    assert (await manage.cancel_appointment_impl(deps, args)).startswith("STATE:needs_confirmation")
    assert writes.cancels == []
    deps["_last_user_turn_at"] = time.time() + 1
    assert (await manage.cancel_appointment_impl(deps, args)).startswith("CANCELLED:")


async def test_cancel_without_a_lookup_does_nothing(writes):
    out = await manage.cancel_appointment_impl(_deps(), {"appointment_token": "appt_made_up"})
    assert out.startswith("STATE:cancel_invalid") and writes.cancels == []


async def test_an_appointment_booked_on_this_call_can_be_moved_and_cancelled(writes):
    deps = _deps()
    token = await _offer(deps)
    await book.book_appointment_impl(deps, {"slot_token": token, "caller_name": "Alex Tan"})
    found = await manage.find_appointment_impl(deps, "someone else entirely")
    assert found.startswith("STATE:appointment_found count=1")  # own booking: no name check
    out = await manage.cancel_appointment_impl(deps, {"appointment_token": _token_in(found)})
    assert out.startswith("CANCELLED:")
    assert deps["_booking_succeeded"] is False  # post-call must not record a booking


# ── which appointments a call may touch ─────────────────────────────────────


def _scope_db(appointments, test_call_ids=("call-test",)):
    return _DB(
        appointments=appointments,
        calls=[{"id": cid} for cid in test_call_ids],
    )


async def test_test_calls_only_see_appointments_made_by_test_calls():
    db = _scope_db([
        _row("real", call_id="call-real"),
        _row("mine", call_id="call-test"),
        _row("orphan", call_id=None),
    ])
    rows = await store.upcoming(_deps(supabase=db))
    assert [r["id"] for r in rows] == ["mine"]


async def test_a_call_cannot_touch_an_event_in_a_calendar_it_may_not_write_to():
    appts = [
        _row("with-event", call_id="call-test", external_event_id="e1", external_event_provider="google"),
        _row("no-event", call_id="call-test"),
    ]
    plain = await store.upcoming(_deps(supabase=_scope_db(appts)))
    assert [r["id"] for r in plain] == ["no-event"]
    demo = await store.upcoming(_deps(supabase=_scope_db(appts), _calendar_provider="google"))
    assert [r["id"] for r in demo] == ["with-event", "no-event"]


async def test_production_calls_only_see_appointments_from_the_callers_number():
    db = _scope_db([
        _row("mine", caller_phone="+6591234567"),
        _row("someone-else", caller_phone="+6500000000"),
        _row("no-phone", caller_phone=""),
    ])
    rows = await store.upcoming(_deps(supabase=db, is_test_call=False, from_number="+6591234567"))
    assert [r["id"] for r in rows] == ["mine"]
    withheld = await store.upcoming(_deps(supabase=db, is_test_call=False, from_number=""))
    assert withheld == []


async def test_move_reports_a_held_slot_as_taken():
    class Overlap(Exception):
        code = "23P01"

    def refuse(query):
        raise Overlap('conflicting key value violates exclusion constraint "appointments_no_overlap"')

    deps = _deps(supabase=_DB(appointments=refuse))
    assert await store.move(deps, _row(), start_utc="a", end_utc="b") == "taken"
    deps = _deps(supabase=_DB(appointments=[]))
    assert await store.move(deps, _row(), start_utc="a", end_utc="b") == "gone"
    deps = _deps(supabase=_DB(appointments=[{"id": "appt-9"}]))
    assert await store.move(deps, _row(), start_utc="a", end_utc="b") == "moved"


# ── calendar sync ───────────────────────────────────────────────────────────


class _FakeCalendar:
    provider = "google"

    def __init__(self, fail=0, delay=0.0):
        self.fail, self.delay, self.calls = fail, delay, []

    def _do(self, name, result=None):
        self.calls.append(name)
        if self.delay:
            time.sleep(self.delay)
        if self.fail > 0:
            self.fail -= 1
            raise CalendarError("boom")
        return result

    def create_event(self, **kwargs):
        return self._do("create", {"provider": "google", "event_id": "evt1", "html_link": "link"})

    def move_event(self, event_id, **kwargs):
        return self._do("move", {"provider": "google", "event_id": event_id, "html_link": "link"})

    def delete_event(self, event_id):
        return self._do("delete")


@pytest.fixture
def sync_env(monkeypatch):
    events, stored = [], []
    monkeypatch.setattr(calendar_sync, "publish_event", lambda deps, kind, **p: events.append((kind, p)))
    monkeypatch.setattr(calendar_sync, "RETRY_DELAY_S", 0.0)

    async def set_event(deps, appointment_id, provider, event_id):
        stored.append(("set", appointment_id, provider, event_id))

    async def move_mirror(deps, event_id, *, start_utc, end_utc):
        stored.append(("move_mirror", event_id))

    async def delete_mirror(deps, event_id):
        stored.append(("delete_mirror", event_id))

    monkeypatch.setattr(store, "set_external_event", set_event)
    monkeypatch.setattr(store, "move_mirror", move_mirror)
    monkeypatch.setattr(store, "delete_mirror", delete_mirror)
    return SimpleNamespace(events=events, stored=stored)


def _appt(**extra):
    return {"id": "appt-1", "caller_name": "Alex Tan", "notes": "product demo",
            "start_time": "2026-11-25T04:00:00+00:00", "end_time": "2026-11-25T05:00:00+00:00",
            "external_event_id": None, "external_event_provider": None, **extra}


async def test_no_calendar_means_no_write(sync_env):
    assert await calendar_sync.create(_deps(), _appt()) == calendar_sync.NONE
    assert sync_env.events == []


async def test_create_saves_the_event_and_reports_it(sync_env):
    cal = _FakeCalendar()
    appt = _appt()
    assert await calendar_sync.create(_deps(_calendar=cal), appt) == calendar_sync.SAVED
    assert sync_env.events == [("calendar_saved", {
        "appointment_id": "appt-1", "provider": "google", "event_id": "evt1", "link": "link",
    })]
    assert sync_env.stored == [("set", "appt-1", "google", "evt1")]
    assert appt["external_event_id"] == "evt1"


async def test_a_failed_write_is_retried_once(sync_env):
    cal = _FakeCalendar(fail=1)
    assert await calendar_sync.create(_deps(_calendar=cal), _appt()) == calendar_sync.SAVED
    assert cal.calls == ["create", "create"]


async def test_two_failures_are_reported_and_never_raise(sync_env):
    cal = _FakeCalendar(fail=2)
    assert await calendar_sync.create(_deps(_calendar=cal), _appt()) == calendar_sync.FAILED
    assert sync_env.events == [("calendar_failed", {
        "appointment_id": "appt-1", "provider": "google", "action": "create",
    })]


async def test_a_slow_write_returns_pending_and_finishes_in_the_background(sync_env, monkeypatch):
    monkeypatch.setattr(calendar_sync, "CALENDAR_WAIT_S", 0.05)
    cal = _FakeCalendar(delay=0.3)
    assert await calendar_sync.create(_deps(_calendar=cal), _appt()) == calendar_sync.PENDING
    assert sync_env.events == []
    await asyncio.sleep(0.5)
    assert [kind for kind, _ in sync_env.events] == ["calendar_saved"]


async def test_move_updates_the_event_and_its_synced_copy(sync_env):
    cal = _FakeCalendar()
    appt = _appt(external_event_id="evt1", external_event_provider="google")
    assert await calendar_sync.move(_deps(_calendar=cal), appt) == calendar_sync.SAVED
    assert cal.calls == ["move"]
    assert sync_env.events[0][0] == "calendar_updated"
    assert sync_env.stored == [("move_mirror", "evt1")]


async def test_move_creates_the_event_when_there_never_was_one(sync_env):
    cal = _FakeCalendar()
    assert await calendar_sync.move(_deps(_calendar=cal), _appt()) == calendar_sync.SAVED
    assert cal.calls == ["create"]


async def test_an_event_in_another_calendar_is_left_alone(sync_env):
    cal = _FakeCalendar()
    appt = _appt(external_event_id="o1", external_event_provider="outlook")
    assert await calendar_sync.move(_deps(_calendar=cal), appt) == calendar_sync.NONE
    assert await calendar_sync.remove(_deps(_calendar=cal), appt) == calendar_sync.NONE
    assert cal.calls == []


async def test_remove_deletes_the_event_and_its_synced_copy(sync_env):
    cal = _FakeCalendar()
    appt = _appt(external_event_id="evt1", external_event_provider="google")
    assert await calendar_sync.remove(_deps(_calendar=cal), appt) == calendar_sync.SAVED
    assert sync_env.events[0][0] == "calendar_removed"
    assert sync_env.stored == [("delete_mirror", "evt1")]


# ── claim guard ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "You're booked for tomorrow at twelve noon.",
    "I've moved your appointment to Thursday at ten.",
    "Your appointment has been cancelled.",
    "That's rescheduled for Friday.",
    "I have cancelled that for you.",
    "Your booking is now confirmed.",
])
def test_statements_that_a_change_is_done_are_claims(text):
    assert guard.claims_change(text)


@pytest.mark.parametrize("text", [
    "Shall I book it?",
    "Would you like that moved to Thursday?",
    "You're all set. Goodbye!",
    "I have two thirty or four o'clock tomorrow.",
    "So that's Alex Tan, tomorrow at four. Is that confirmed?",
    "",
])
def test_questions_offers_and_goodbyes_are_not_claims(text):
    assert not guard.claims_change(text)


def _stream(*parts):
    async def gen():
        for part in parts:
            yield part
    return gen()


async def _collect(deps, replies):
    """replies: list of chunk tuples, one per generation. Returns (spoken, runs)."""
    runs = []

    def run(tool_choice, nudge):
        runs.append((tool_choice, nudge))
        return _stream(*replies[len(runs) - 1])

    spoken = [chunk async for chunk in guard.guarded_llm_stream(deps, run)]
    return "".join(spoken), runs


_RETURNED = {"_last_tool_state": "STATE:slot_ok"}  # a calendar tool has returned


async def test_guard_is_idle_when_a_tool_has_returned_and_nothing_is_on_offer():
    spoken, runs = await _collect(dict(_RETURNED), [("You're booked for noon.",)])
    assert spoken == "You're booked for noon." and runs == [(None, None)]


@pytest.mark.parametrize("text", [
    "Let me check that for you. I have you down for tomorrow at two o'clock in the afternoon.",
    "I have two thirty or four o'clock tomorrow.",
    "You're booked for tomorrow at 2 PM.",
    "Sure, one moment.",
    "Tomorrow at twelve noon works.",
])
def test_calendar_talk_before_any_tool_is_ungrounded(text):
    assert guard.ungrounded(text)


@pytest.mark.parametrize("text", [
    "What day and time works for you?",
    "Thank you, Alex. What day works for the product demo?",
    "We're open eight AM to five PM, Monday to Friday.",
    "You're welcome, have a great day. Goodbye.",
    "",
])
def test_ordinary_first_turn_replies_are_grounded(text):
    assert not guard.ungrounded(text)


async def test_made_up_availability_before_any_tool_is_dropped_and_a_tool_required():
    deps = {}
    spoken, runs = await _collect(deps, [
        ("Let me check that for you. ", "I have you down for two o'clock."),
        ("(check_day)",),
    ])
    assert spoken == "(check_day)"
    assert runs == [(None, None), ("required", guard.GROUNDING_NUDGE)]
    assert deps["_claim_guard_trips"] == 1


async def test_first_turn_replies_and_tool_calls_pass_untouched():
    spoken, runs = await _collect({}, [("What day ", "works for you?")])
    assert spoken == "What day works for you?" and len(runs) == 1
    tool_chunk = SimpleNamespace(delta=SimpleNamespace(content="", tool_calls=[object()]))
    runs = []

    def run(tool_choice, nudge):
        runs.append(tool_choice)
        return _stream(tool_chunk, "after")

    out = [c async for c in guard.guarded_llm_stream({}, run)]
    assert out == [tool_chunk, "after"] and runs == [None]


async def test_a_false_booking_claim_is_dropped_and_the_tool_forced():
    deps = {**_RETURNED, "_slot_tokens": {"slot_a": {"created_at": time.time()}}}
    spoken, runs = await _collect(deps, [("You're booked ", "for noon."), ("(tool call)",)])
    assert spoken == "(tool call)"
    assert runs[1] == (guard.FORCED_TOOL_CHOICE, None)
    assert deps["_claim_guard_trips"] == 1


async def test_with_an_existing_appointment_in_play_no_tool_is_forced():
    deps = {**_RETURNED, "_appt_tokens": {"appt_a": {"created_at": time.time(), "appointment": {"id": "x"}}}}
    spoken, runs = await _collect(deps, [("I've moved it to Thursday.",), ("Shall I move it?",)])
    assert spoken == "Shall I move it?"
    assert runs[1] == (None, guard.NUDGE)


async def test_guard_stands_down_after_a_completed_write():
    deps = {
        **_RETURNED,
        "_slot_tokens": {"slot_a": {"created_at": time.time() - 10}},
        "_last_write_at": time.time(),
    }
    spoken, runs = await _collect(deps, [("You're booked for noon.",)])
    assert spoken == "You're booked for noon." and len(runs) == 1


@pytest.mark.parametrize("text", [
    "A pleasure. I can offer you Monday the thirtieth, Tuesday the first of December, or Wednesday the second.",
    "We have availability tomorrow and Thursday.",
    "Understood, Alex. I'll go ahead and book your product demo for Monday.",
])
def test_offered_days_and_promises_before_any_tool_are_ungrounded(text):
    assert guard.ungrounded(text)


@pytest.mark.parametrize("text,expected", [
    ("Understood, Alex. I'll go ahead and book your product demo for Monday the thirtieth.", True),
    ("Okay, booking that in for you now.", True),
    ("I will cancel that for you.", True),
    ("Shall I book it?", False),
    ("I'll book it for you, shall I?", False),
    ("What day works for you?", False),
])
def test_a_promise_to_act_is_recognised(text, expected):
    assert guard.promises_change(text) is expected


async def test_a_promise_with_no_tool_call_is_dropped_and_answered_again():
    # Production call, 2026-11-24: "I'll go ahead and book ..." and nothing was booked.
    deps = {**_RETURNED, "_slot_tokens": {"slot_a": {"created_at": time.time()}}}
    spoken, runs = await _collect(deps, [
        ("Understood. ", "I'll go ahead and book ", "your demo for Monday. Anything else?"),
        ("(book_appointment)",),
    ])
    assert spoken == "(book_appointment)"
    assert runs == [(None, None), (None, guard.PROMISE_NUDGE)]
    assert deps["_claim_guard_trips"] == 1


async def test_a_promise_followed_by_its_tool_call_passes():
    deps = {**_RETURNED, "_slot_tokens": {"slot_a": {"created_at": time.time()}}}
    tool_chunk = SimpleNamespace(delta=SimpleNamespace(content="", tool_calls=[object()]))
    runs = []

    def run(tool_choice, nudge):
        runs.append((tool_choice, nudge))
        return _stream("I'll book that now.", tool_chunk)

    out = [c async for c in guard.guarded_llm_stream(deps, run)]
    assert out == ["I'll book that now.", tool_chunk] and len(runs) == 1


async def test_guard_is_quiet_right_after_a_real_booking(writes):
    # Seen on the first live call: "You're all booked" after book_appointment
    # had returned BOOKED was dropped and re-generated.
    deps = _deps()
    token = await _offer(deps)
    assert guard.guard_active(deps)
    await book.book_appointment_impl(deps, {"slot_token": token, "caller_name": "Alex"})
    assert not guard.guard_active(deps)
    spoken, runs = await _collect(deps, [("You're all booked for Wednesday.",)])
    assert spoken == "You're all booked for Wednesday." and len(runs) == 1


async def test_guard_passes_ordinary_replies_and_gives_up_after_two_trips():
    deps = {**_RETURNED, "_slot_tokens": {"slot_a": {"created_at": time.time()}}}
    spoken, runs = await _collect(deps, [("I have two thirty or four.",)])
    assert spoken == "I have two thirty or four." and len(runs) == 1
    deps["_claim_guard_trips"] = guard.MAX_TRIPS
    spoken, runs = await _collect(deps, [("You're booked.",)])
    assert spoken == "You're booked." and len(runs) == 1


# ── speech before a tool call ───────────────────────────────────────────────


def _text(content):
    return SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=[]), usage=None)


def _call(name, content=None):
    return SimpleNamespace(
        delta=SimpleNamespace(content=content, tool_calls=[SimpleNamespace(name=name)]), usage=None,
    )


async def _spoken_and_calls(*chunks):
    from src.agents.appointments.preamble import strip_tool_preamble

    spoken, calls = "", []
    async for chunk in strip_tool_preamble(_stream(*chunks)):
        spoken += chunk.delta.content or ""
        calls += [c.name for c in chunk.delta.tool_calls]
    return spoken, calls


async def test_speech_before_a_lookup_or_a_write_is_dropped():
    assert await _spoken_and_calls(_text("Let me check "), _text("that for you."), _call("check_day")) == (
        "", ["check_day"],
    )
    assert await _spoken_and_calls(_text("[assistant]"), _call("book_appointment")) == ("", ["book_appointment"])
    assert await _spoken_and_calls(_call("find_appointment", content="One moment.")) == ("", ["find_appointment"])


async def test_the_goodbye_before_end_call_is_kept():
    spoken, calls = await _spoken_and_calls(_text("Thanks for calling. "), _text("Goodbye."), _call("end_call"))
    assert spoken == "Thanks for calling. Goodbye." and calls == ["end_call"]


async def test_ordinary_replies_pass_unchanged():
    spoken, calls = await _spoken_and_calls(_text("Sure. "), _text("What day "), _text("works for you?"))
    assert spoken == "Sure. What day works for you?" and calls == []
    assert await _spoken_and_calls(_text("What day works for you?")) == ("What day works for you?", [])


async def test_a_reply_that_is_only_a_role_marker_is_not_spoken():
    assert await _spoken_and_calls(_text("[assistant]")) == ("", [])
    assert await _spoken_and_calls(_text("Assistant:")) == ("", [])


async def test_speech_after_a_tool_call_is_not_touched():
    spoken, calls = await _spoken_and_calls(_call("check_day"), _text("I have two or four."))
    assert spoken == "I have two or four." and calls == ["check_day"]


def test_the_agent_applies_the_preamble_filter_to_every_reply():
    src = (_SRC / "agents" / "appointments" / "entry.py").read_text(encoding="utf-8")
    assert "strip_tool_preamble(guarded_llm_stream(self._deps, _run), self._deps)" in src


# ── which model runs a call ─────────────────────────────────────────────────


def _cfg(shared="groq"):
    return SimpleNamespace(LLM_PROVIDER=shared)


def test_model_choice_order_is_demo_then_agent_setting_then_shared(monkeypatch):
    from src.agents.appointments.pipeline import resolve_provider

    monkeypatch.setenv("GROQ_API_KEY", "key")
    monkeypatch.delenv("VOCO_APPT_LLM_PROVIDER", raising=False)
    assert resolve_provider(_cfg("groq")) == "groq"
    assert resolve_provider(_cfg("openai")) == "openai"
    monkeypatch.setenv("VOCO_APPT_LLM_PROVIDER", "openai")
    assert resolve_provider(_cfg("groq")) == "openai"          # production setting for this agent
    assert resolve_provider(_cfg("groq"), "groq") == "groq"    # the Demo Studio's choice wins
    monkeypatch.setenv("VOCO_APPT_LLM_PROVIDER", "groq")
    assert resolve_provider(_cfg("openai"), " OpenAI ") == "openai"


def test_unknown_model_names_are_skipped_and_groq_needs_its_key(monkeypatch):
    from src.agents.appointments.pipeline import resolve_provider

    monkeypatch.setenv("GROQ_API_KEY", "key")
    monkeypatch.setenv("VOCO_APPT_LLM_PROVIDER", "nonsense")
    assert resolve_provider(_cfg("groq"), "claude") == "groq"
    assert resolve_provider(_cfg("something"), None) == "openai"
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert resolve_provider(_cfg("groq"), "groq") == "openai"


def test_only_test_calls_can_name_a_model():
    src = (_SRC / "agents" / "appointments" / "entry.py").read_text(encoding="utf-8")
    assert 'resolve_provider(base, room_meta.get("llm") if is_test_call else None)' in src
    assert "model=llm_provider" in src and "build_llm(base, tenant_id, llm_provider)" in src


def test_each_model_gets_its_own_closing_notes():
    shared = build_prompt(business_name="Voco", working_hours=HOURS, tenant_timezone=TZ)
    groq = build_prompt(business_name="Voco", working_hours=HOURS, tenant_timezone=TZ, model="groq")
    gpt = build_prompt(business_name="Voco", working_hours=HOURS, tenant_timezone=TZ, model="openai")
    assert "REMEMBER" not in shared
    assert "A name is all you need to look an appointment up." in groq
    assert "go in the same response" in gpt and "go in the same response" not in groq
    # Everything before the notes is identical, and the hours still close the prompt.
    cut = shared.index("BUSINESS HOURS")
    assert groq.startswith(shared[:cut]) and gpt.startswith(shared[:cut])
    assert groq.rstrip().endswith(shared[cut:].rstrip())


# ── wiring ──────────────────────────────────────────────────────────────────


def test_entry_runs_the_shared_post_call_pipeline_without_triage():
    src = (_SRC / "agents" / "appointments" / "entry.py").read_text(encoding="utf-8")
    assert '"skip_triage": True' in src
    assert '"is_test_call": is_test_call' in src and '"demo": demo' in src
    post = (_SRC / "post_call.py").read_text(encoding="utf-8")
    assert 'if not params.get("skip_triage"):' in post


def test_entry_uses_the_shared_voice_tuning_not_a_copy():
    src = (_SRC / "agents" / "appointments" / "pipeline.py").read_text(encoding="utf-8")
    # Every shared switch is read once, in src/agent.py. The one switch read
    # here is this agent's own model override.
    assert src.count('os.environ.get("VOCO_') == 1 and "VOCO_APPT_LLM_PROVIDER" in src
    for name in ("LLM_MODEL", "GROQ_LLM_MODEL", "LLM_PROVIDER", "ENDPOINTING_MODE", "PREEMPTIVE_TTS"):
        assert f"cfg.{name}" in src


def test_register_appointment_reuses_one_token_per_appointment():
    deps = {}
    first = register_appointment(deps, {"id": "a", "start_time": "x"})
    again = register_appointment(deps, {"id": "a", "start_time": "y"})
    assert first == again and deps["_appt_tokens"][first]["appointment"]["start_time"] == "y"


def test_prompt_moves_in_one_step_when_the_new_time_was_given():
    """Production calls on 2026-11-24: asked to "make it Thursday at ten
    instead", Qwen answered "where would you like it instead?" and
    gpt-4.1-mini asked for confirmation twice."""
    prompt = build_prompt(business_name="Acme Dental", working_hours=HOURS, tenant_timezone=TZ)
    assert "If the caller already said the new time" in prompt
    assert "(find_appointment) (check_slot)" in prompt


def test_find_appointment_sends_a_named_new_time_straight_to_check_slot():
    src = (_SRC / "agents" / "appointments" / "tools" / "manage.py").read_text(encoding="utf-8")
    assert "if the caller already named the new time they want, say nothing" in src


# ── "tomorrow at two" passed as 2:00 ────────────────────────────────────────


def test_a_time_before_opening_is_read_as_the_afternoon():
    tenant = {"working_hours": HOURS}  # 08:00 to 17:00 every day
    date = "2026-11-25"
    fix = availability.afternoon_if_before_opening
    assert fix(tenant, date, "2:00") == "14:00"
    assert fix(tenant, date, "02:30") == "14:30"
    assert fix(tenant, date, "4:00") == "16:00"
    # Real morning times, afternoon times and times shut either way are untouched.
    for unchanged in ("8:00", "09:00", "11:30", "14:00", "5:00", "6:00", "7:00", "bad"):
        assert fix(tenant, date, unchanged) == unchanged
    # No hours for that day, or the day is closed: nothing to go on.
    assert fix({}, date, "2:00") == "2:00"
    closed = {"working_hours": {"wednesday": {"enabled": False, "open": "08:00", "close": "17:00"}}}
    assert fix(closed, date, "2:00") == "2:00"


@pytest.mark.asyncio
async def test_check_slot_takes_two_as_two_in_the_afternoon(monkeypatch):
    deps = _deps()
    date = _day(2)
    plain = await availability.check_slot_impl(deps, date, "14:00")
    short = await availability.check_slot_impl(_deps(), date, "2:00")
    assert plain.startswith("STATE:slot_ok") and short.startswith("STATE:slot_ok")
    assert "2:00 PM" in short
