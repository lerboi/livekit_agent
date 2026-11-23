"""Demo Studio invariants (2026-11-23).

A demo call is a sandboxed test call whose server-set room metadata carries a
`demo` block. It may relax exactly two sandbox rules: push the booking to the
external calendar it names, and keep the appointment after the call. Everything
else a test call is protected from stays off: caller SMS, recovery SMS, usage
billing, CRM writes, owner notifications.

Behavioural tests drive `_book_appointment_impl` with the database, the atomic
booking RPC, the calendar push and the SMS sender mocked. Post-call invariants
that would need the whole pipeline mocked are source checks, the same shape as
tests/test_test_call_sandbox.py.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.lib import calendar_push
from src.lib.calendar_text import SCHEDULING_BODY, scheduling_event_text
from src.lib.demo import (
    EVENTS_TOPIC,
    demo_external_destination,
    keeps_booking,
    parse_demo,
    publish_event,
)
from src.tools import book_appointment as book_module
from src.tools.book_appointment import _book_appointment_impl

_SRC = Path(__file__).parent.parent / "src"

SLOT_START = "2026-12-01T06:00:00+00:00"
SLOT_END = "2026-12-01T07:00:00+00:00"


def _read(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


# ── parse_demo: the block is honoured only on test calls ────────────────────


def test_demo_ignored_unless_test_call():
    meta = {"demo": {"task": "scheduling", "destination": "google", "keep_booking": True}}
    assert parse_demo(meta, is_test_call=False) is None


def test_demo_parsed_on_test_call():
    meta = {
        "test_call": True,
        "demo": {"task": "Scheduling", "destination": "Google", "keep_booking": True},
    }
    assert parse_demo(meta, is_test_call=True) == {
        "task": "scheduling",
        "destination": "google",
        "keep_booking": True,
    }


@pytest.mark.parametrize("raw", [None, "google", ["google"], 1, True])
def test_demo_block_must_be_an_object(raw):
    assert parse_demo({"demo": raw}, is_test_call=True) is None


def test_demo_absent_or_bad_metadata():
    assert parse_demo({}, is_test_call=True) is None
    assert parse_demo(None, is_test_call=True) is None


def test_demo_unknown_destination_falls_back_to_voco_only():
    demo = parse_demo({"demo": {"destination": "salesforce"}}, is_test_call=True)
    assert demo["destination"] == "voco"
    assert demo["keep_booking"] is False


@pytest.mark.parametrize("value", ["true", 1, "yes", None])
def test_keep_booking_requires_literal_true(value):
    demo = parse_demo({"demo": {"destination": "google", "keep_booking": value}}, True)
    assert demo["keep_booking"] is False


def test_external_destination_needs_test_call_and_external_provider():
    google = {"destination": "google", "keep_booking": True}
    assert demo_external_destination({"is_test_call": True, "demo": google}) == "google"
    assert demo_external_destination({"is_test_call": True, "demo": {"destination": "voco"}}) is None
    assert demo_external_destination({"is_test_call": True, "demo": None}) is None
    # A demo block on a call that is not a test call changes nothing.
    assert demo_external_destination({"is_test_call": False, "demo": google}) is None


def test_keeps_booking_needs_test_call():
    demo = {"destination": "google", "keep_booking": True}
    assert keeps_booking(demo, True) is True
    assert keeps_booking(demo, False) is False
    assert keeps_booking({"destination": "google", "keep_booking": False}, True) is False
    assert keeps_booking(None, True) is False


def test_entrypoint_parses_demo_from_metadata_gated_on_test_call():
    src = _read("agent.py")
    assert "demo = parse_demo(room_meta, is_test_call)" in src
    assert '"demo": demo,' in src
    assert 'is_web_test = is_test_call and room_meta.get("web_test") is True' in src
    assert '"_events_room": ctx.room if is_web_test else None' in src


# ── book_appointment: what fires for each kind of call ──────────────────────


def _supabase_mock() -> MagicMock:
    sb = MagicMock()
    update_chain = sb.table.return_value.update.return_value.eq.return_value
    update_chain.is_.return_value.execute.return_value = MagicMock(data=[])
    update_chain.execute.return_value = MagicMock(data=[{"call_id": "c1"}])
    return sb


def _deps(**overrides) -> dict:
    deps = {
        "supabase": _supabase_mock(),
        "tenant": {
            "tenant_timezone": "Asia/Singapore",
            "business_name": "Voco",
            "default_locale": "en",
            "slot_duration_mins": 60,
        },
        "tenant_id": "tenant-1",
        "tenant_timezone": "Asia/Singapore",
        "call_id": "test-web-tenant-1-1",
        "call_uuid": "call-uuid-1",
        "from_number": "+15551234567",
        "to_number": "+15557654321",
        "agent_profile": "scheduling",
        "is_test_call": False,
        "demo": None,
        "_slot_tokens": {
            "slot_abc": {
                "slot_start_utc": SLOT_START,
                "slot_end_utc": SLOT_END,
                "created_at": time.time() - 30,
            }
        },
    }
    deps.update(overrides)
    return deps


ARGS = {"slot_token": "slot_abc", "caller_name": "Alex Tan", "reason": "product demo"}


@pytest.fixture
def effects():
    """Mocks for every external side effect of a successful booking."""
    with patch.object(book_module, "atomic_book_slot", new_callable=AsyncMock) as atomic, \
         patch.object(book_module, "push_booking_to_calendar") as push, \
         patch.object(book_module, "send_caller_sms") as sms, \
         patch.object(book_module, "send_caller_recovery_sms") as recovery_sms:
        atomic.return_value = {"success": True, "appointment_id": "appt-1"}
        push.return_value = {
            "provider": "google",
            "event_id": "evt-1",
            "html_link": "https://calendar.example/evt-1",
        }
        yield SimpleNamespace(atomic=atomic, push=push, sms=sms, recovery_sms=recovery_sms)


async def _book(deps) -> str:
    result = await _book_appointment_impl(deps, dict(ARGS), MagicMock())
    # Calendar push and SMS are fire-and-forget background tasks.
    for _ in range(20):
        await asyncio.sleep(0.01)
    return result


async def test_production_call_pushes_to_primary_and_texts_caller(effects):
    result = await _book(_deps())
    assert result.startswith("BOOKED")
    effects.push.assert_called_once()
    assert effects.push.call_args.kwargs == {"provider": None, "style": "scheduling"}
    effects.sms.assert_called_once()


async def test_plain_test_call_has_no_external_side_effects(effects):
    result = await _book(_deps(is_test_call=True))
    assert result.startswith("BOOKED")
    effects.push.assert_not_called()
    effects.sms.assert_not_called()


async def test_demo_call_pushes_to_chosen_calendar_and_never_texts(effects):
    demo = {"task": "scheduling", "destination": "google", "keep_booking": True}
    result = await _book(_deps(is_test_call=True, demo=demo))
    assert result.startswith("BOOKED")
    effects.push.assert_called_once()
    assert effects.push.call_args.args == ("tenant-1", "appt-1", "Asia/Singapore")
    assert effects.push.call_args.kwargs == {"provider": "google", "style": "scheduling"}
    effects.sms.assert_not_called()
    effects.recovery_sms.assert_not_called()


async def test_demo_call_with_voco_only_destination_does_not_push(effects):
    demo = {"task": "scheduling", "destination": "voco", "keep_booking": True}
    await _book(_deps(is_test_call=True, demo=demo))
    effects.push.assert_not_called()
    effects.sms.assert_not_called()


async def test_demo_block_without_test_call_is_an_ordinary_call(effects):
    """Belt and braces: parse_demo already drops the block on a non-test call.
    If one reached the tool anyway it must not steer the push."""
    demo = {"task": "scheduling", "destination": "outlook", "keep_booking": True}
    await _book(_deps(is_test_call=False, demo=demo))
    assert effects.push.call_args.kwargs["provider"] is None


async def test_slot_taken_on_demo_call_never_sends_recovery_sms(effects):
    effects.atomic.return_value = {"success": False}
    demo = {"task": "scheduling", "destination": "google", "keep_booking": True}
    deps = _deps(is_test_call=True, demo=demo)
    with patch.object(book_module, "calculate_available_slots", return_value=[]):
        result = await _book(deps)
    assert result.startswith("STATE:slot_taken")
    effects.recovery_sms.assert_not_called()
    effects.push.assert_not_called()


def test_sandbox_gates_precede_side_effects_in_source():
    src = _read("tools/book_appointment.py")
    plain_skip = src.find("if is_test_call and not demo_destination:")
    push = src.find("create_background_task(_push_calendar_bg())")
    sms_skip = src.rfind("    if is_test_call:\n")
    sms = src.find("create_background_task(_send_confirmation_sms_bg())")
    assert -1 not in (plain_skip, push, sms_skip, sms)
    assert plain_skip < push < sms_skip < sms


# ── live events ─────────────────────────────────────────────────────────────


class _FakeRoom:
    def __init__(self):
        self.sent = []
        self.local_participant = SimpleNamespace(send_text=self._send_text)

    async def _send_text(self, text, *, topic=""):
        self.sent.append((topic, json.loads(text)))


async def test_events_published_for_web_test_rooms(effects):
    room = _FakeRoom()
    demo = {"task": "scheduling", "destination": "google", "keep_booking": True}
    await _book(_deps(is_test_call=True, demo=demo, _events_room=room))
    assert {topic for topic, _ in room.sent} == {EVENTS_TOPIC}
    by_type = {event["type"]: event for _, event in room.sent}
    assert by_type["booked"]["appointment_id"] == "appt-1"
    assert by_type["booked"]["start"] == SLOT_START
    assert by_type["booked"]["reason"] == "product demo"
    assert by_type["calendar_saved"]["provider"] == "google"
    assert by_type["calendar_saved"]["link"] == "https://calendar.example/evt-1"


async def test_calendar_failure_is_reported_as_an_event(effects):
    effects.push.return_value = None
    room = _FakeRoom()
    demo = {"task": "scheduling", "destination": "google", "keep_booking": True}
    await _book(_deps(is_test_call=True, demo=demo, _events_room=room))
    types = [event["type"] for _, event in room.sent]
    assert "calendar_failed" in types and "calendar_saved" not in types


async def test_no_events_without_an_events_room(effects):
    # Phone calls and non-web test calls have no events room: nothing is sent
    # and nothing raises.
    publish_event({}, "booked", appointment_id="x")
    publish_event({"_events_room": None}, "booked", appointment_id="x")
    await _book(_deps())


async def test_event_send_failure_never_reaches_the_call():
    room = SimpleNamespace(
        local_participant=SimpleNamespace(send_text=AsyncMock(side_effect=RuntimeError("gone")))
    )
    publish_event({"_events_room": room}, "booked", appointment_id="x")
    await asyncio.sleep(0.02)


# ── confirm-then-book gate ──────────────────────────────────────────────────


async def test_gate_blocks_booking_offered_after_the_callers_last_turn(effects):
    deps = _deps(_last_user_turn_at=time.time() - 60)  # token was minted 30 s ago
    first = await _book(deps)
    assert first.startswith("STATE:needs_confirmation")
    effects.atomic.assert_not_called()
    # The caller answers; the same token now books. The gate fires once per slot.
    deps["_last_user_turn_at"] = time.time()
    second = await _book(deps)
    assert second.startswith("BOOKED")
    effects.atomic.assert_called_once()


async def test_gate_never_blocks_the_same_slot_twice(effects):
    deps = _deps(_last_user_turn_at=time.time() - 60)
    assert (await _book(deps)).startswith("STATE:needs_confirmation")
    assert (await _book(deps)).startswith("BOOKED")


async def test_gate_passes_when_caller_spoke_after_the_offer(effects):
    result = await _book(_deps(_last_user_turn_at=time.time()))
    assert result.startswith("BOOKED")


async def test_gate_uses_first_offer_of_the_slot_not_a_recheck(effects):
    """The model re-checking the slot just before booking mints a newer token
    for the same time. The caller already confirmed that time, so no block."""
    deps = _deps(_last_user_turn_at=time.time() - 10)
    deps["_slot_tokens"]["slot_new"] = {
        "slot_start_utc": SLOT_START,
        "slot_end_utc": SLOT_END,
        "created_at": time.time(),
    }
    result = await _book_appointment_impl(
        deps, {**ARGS, "slot_token": "slot_new"}, MagicMock()
    )
    assert result.startswith("BOOKED")


async def test_gate_inactive_without_a_recorded_caller_turn(effects):
    assert (await _book(_deps(_last_user_turn_at=None))).startswith("BOOKED")


async def test_gate_can_be_switched_off(effects, monkeypatch):
    monkeypatch.setattr(book_module, "CONFIRM_GATE_ENABLED", False)
    result = await _book(_deps(_last_user_turn_at=time.time() - 60))
    assert result.startswith("BOOKED")


async def test_gate_is_scheduling_profile_only(effects):
    with patch.object(
        book_module, "validate_address_with_region_fallback", new_callable=AsyncMock
    ) as validate:
        validate.return_value = ({"verdict": "unconfirmed"}, "US")
        deps = _deps(agent_profile="home_service", _last_user_turn_at=time.time() - 60)
        args = {**ARGS, "street_name": "1 Main St", "postal_code": "12345"}
        result = await _book_appointment_impl(deps, args, MagicMock())
    assert result.startswith("BOOKED")


# ── calendar push: explicit provider, event wording ─────────────────────────


def _creds_supabase(connected: list[str], primary: str | None = None) -> MagicMock:
    """calendar_credentials lookups filtered by the .eq() calls made."""
    sb = MagicMock()

    class _Query:
        def __init__(self):
            self.filters = {}

        def select(self, *_):
            return self

        def eq(self, column, value):
            self.filters[column] = value
            return self

        def limit(self, *_):
            return self

        def execute(self):
            rows = [{"provider": p} for p in connected]
            if "provider" in self.filters:
                rows = [r for r in rows if r["provider"] == self.filters["provider"]]
            if self.filters.get("is_primary"):
                rows = [r for r in rows if r["provider"] == primary]
            return MagicMock(data=rows[:1])

    sb.table.side_effect = lambda _name: _Query()
    return sb


@pytest.fixture
def providers():
    with patch.object(calendar_push, "push_google") as google, \
         patch.object(calendar_push, "push_booking_to_outlook") as outlook:
        google.return_value = {"provider": "google", "event_id": "g1", "html_link": "g-link"}
        outlook.return_value = {"provider": "outlook", "event_id": "o1", "html_link": "o-link"}
        yield SimpleNamespace(google=google, outlook=outlook)


def _push(sb, **kwargs):
    with patch.object(calendar_push, "get_supabase_admin", return_value=sb):
        return calendar_push.push_booking_to_calendar("t1", "a1", "Asia/Singapore", **kwargs)


def test_explicit_provider_is_used_even_when_another_is_primary(providers):
    sb = _creds_supabase(["google", "outlook"], primary="outlook")
    result = _push(sb, provider="google", style="scheduling")
    assert result["html_link"] == "g-link"
    providers.google.assert_called_once_with("t1", "a1", "Asia/Singapore", style="scheduling")
    providers.outlook.assert_not_called()


def test_explicit_provider_not_connected_does_not_fall_back(providers):
    sb = _creds_supabase(["outlook"], primary="outlook")
    assert _push(sb, provider="google") is None
    providers.google.assert_not_called()
    providers.outlook.assert_not_called()


def test_unknown_explicit_provider_pushes_nowhere(providers):
    sb = _creds_supabase(["google"], primary="google")
    assert _push(sb, provider="salesforce") is None
    providers.google.assert_not_called()


def test_default_uses_primary_then_any(providers):
    _push(_creds_supabase(["google", "outlook"], primary="outlook"))
    providers.outlook.assert_called_once_with("t1", "a1", style=None)
    providers.google.assert_not_called()
    _push(_creds_supabase(["google"], primary=None))
    providers.google.assert_called_once()


def test_no_calendar_connected_returns_none(providers):
    assert _push(_creds_supabase([])) is None


def test_scheduling_event_text():
    title, body = scheduling_event_text({"caller_name": "Alex Tan", "notes": "product demo"})
    assert title == "Alex Tan: Product demo"
    assert body == SCHEDULING_BODY == "Booked by the Voco voice agent"
    assert scheduling_event_text({"caller_name": "Alex Tan", "notes": None})[0] == "Alex Tan"
    assert scheduling_event_text({"caller_name": "Caller", "notes": "intro call"})[0] == "Intro call"
    assert scheduling_event_text({"caller_name": "", "notes": ""})[0] == "Appointment"


def _google_event_body(style):
    """Run the Google push with everything external mocked; return the body
    passed to events.insert()."""
    from src.lib import google_calendar

    sb = MagicMock()
    rows = {
        "calendar_credentials": [{
            "access_token": "tok", "refresh_token": "ref",
            "expiry_date": 0, "calendar_id": "primary",
        }],
        "appointments": [{
            "start_time": SLOT_START, "end_time": SLOT_END,
            "service_address": "Not applicable", "caller_name": "Alex Tan",
            "urgency": "routine", "notes": "product demo",
        }],
        "tenants": [{"business_name": "Voco"}],
    }

    def _table(name):
        chain = MagicMock()
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.limit.return_value = chain
        chain.update.return_value = chain
        chain.execute.return_value = MagicMock(data=rows.get(name, []))
        return chain

    sb.table.side_effect = _table
    service = MagicMock()
    service.events.return_value.insert.return_value.execute.return_value = {
        "id": "evt-1", "htmlLink": "https://calendar.example/evt-1",
    }
    creds = MagicMock(token="tok", refresh_token="ref")
    with patch.object(google_calendar, "get_supabase_admin", return_value=sb), \
         patch.object(google_calendar, "build", return_value=service), \
         patch.object(google_calendar, "Credentials", return_value=creds):
        result = google_calendar.push_booking_to_calendar(
            "t1", "a1", "Asia/Singapore", style=style
        )
    return result, service.events.return_value.insert.call_args.kwargs["body"]


def test_google_scheduling_event_has_no_address_or_urgency():
    result, body = _google_event_body("scheduling")
    assert body["summary"] == "Alex Tan: Product demo"
    assert body["description"] == "Booked by the Voco voice agent"
    assert "Service Address" not in body["description"]
    assert "Urgency" not in body["description"]
    assert body["start"] == {"dateTime": "2026-12-01T14:00:00", "timeZone": "Asia/Singapore"}
    assert result == {
        "provider": "google",
        "event_id": "evt-1",
        "html_link": "https://calendar.example/evt-1",
    }


def test_google_home_service_event_is_unchanged():
    _, body = _google_event_body(None)
    assert body["summary"] == "Alex Tan - Voco"
    assert "Service Address:" in body["description"]
    assert "Urgency:" in body["description"]


# ── post-call: keep the booking, keep every other protection ────────────────


def test_post_call_auto_cancel_skipped_only_for_keep_booking():
    src = _read("post_call.py")
    assert '_keep_demo_booking = keeps_booking(params.get("demo"), is_test_call)' in src
    assert "if is_test_call and tenant_id and not _keep_demo_booking:" in src


def test_post_call_billing_crm_and_notifications_do_not_look_at_demo():
    """The demo block relaxes the auto-cancel and nothing else: the billing,
    CRM and owner-notification gates still key on is_test_call alone."""
    src = _read("post_call.py")
    assert "if not is_test_call and tenant_id and duration_seconds >= MIN_BILLABLE_DURATION_SEC:" in src
    assert "elif call_uuid and duration_seconds >= MIN_BILLABLE_DURATION_SEC:" in src
    assert src.count("_keep_demo_booking") == 3  # assignment, its log line, the one gate
    assert src.count('params.get("demo")') == 1


def test_agent_forwards_demo_to_post_call():
    assert '"demo": demo,' in _read("agent.py")
