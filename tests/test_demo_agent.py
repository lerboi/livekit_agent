"""The demo agent (src/agents/demo): the clinic front desk for the recorded demo.

Covers who can reach it, its prompt, and the rule that a plain "yes" to a time
on offer must make the change (a spoken take on 2026-11-24 answered "Yes,
please" with "What is the appointment for?" and booked nothing).
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agents import APPOINTMENTS, DEMO, RECEPTIONIST, select_agent
from src.agents.demo.entry import PROFILE
from src.agents.demo.policy import confirm_pending_write, is_plain_yes, pending_tool
from src.agents.demo.prompt import SERVICES, build_prompt

_SRC = Path(__file__).parent.parent / "src"


def _ctx(*items):
    return SimpleNamespace(items=list(items))


def _user(text):
    return SimpleNamespace(type="message", role="user", text_content=text)


def _agent(text):
    return SimpleNamespace(type="message", role="assistant", text_content=text)


def _offered(**extra):
    deps = {"_slot_tokens": {"slot_a": {"slot_start_utc": "2026-11-25T06:00:00+00:00"}}, "_last_offered_token": "slot_a"}
    deps.update(extra)
    return deps


# ── who gets the demo agent ─────────────────────────────────────────────────


def test_only_a_test_call_can_ask_for_the_demo_agent(monkeypatch):
    monkeypatch.delenv("VOCO_DEFAULT_AGENT", raising=False)
    assert select_agent({"agent": "demo"}, True) == DEMO
    assert select_agent({"agent": "demo"}, False) == RECEPTIONIST


def test_the_demo_agent_cannot_be_made_the_default(monkeypatch):
    monkeypatch.setenv("VOCO_DEFAULT_AGENT", "demo")
    assert select_agent({}, False) == RECEPTIONIST
    monkeypatch.setenv("VOCO_DEFAULT_AGENT", "appointments")
    assert select_agent({}, False) == APPOINTMENTS


def test_router_hands_demo_calls_to_the_demo_package():
    src = (_SRC / "agent.py").read_text(encoding="utf-8")
    assert "if agent_kind in (APPOINTMENTS, DEMO):" in src
    assert "from .agents.demo.entry import run_call as run_appointments_call" in src


def test_demo_calls_run_on_the_shared_call_code_with_their_own_profile():
    src = (_SRC / "agents" / "demo" / "entry.py").read_text(encoding="utf-8")
    assert "await run_appointments_call(ctx, base, profile=PROFILE, **call)" in src
    assert PROFILE["agent_name"] == "demo"
    assert PROFILE["llm"] == "openai"
    assert PROFILE["build_prompt"] is build_prompt
    assert PROFILE["turn_policy"] is confirm_pending_write
    assert "cleaning" in PROFILE["keyterms"]


# ── prompt ──────────────────────────────────────────────────────────────────


def test_prompt_is_a_clinic_front_desk_that_never_asks_the_reason():
    prompt = build_prompt(business_name="Brightside Dental")
    assert "You are the front desk of Brightside Dental, a dental clinic" in prompt
    assert "NEVER ask what the appointment is for." in prompt
    assert "call book_appointment at once" in prompt
    assert "call reschedule_appointment at once" in prompt
    for service in SERVICES:
        assert service in prompt.lower()
    for tool in ("check_slot", "check_day", "next_available_days", "book_appointment",
                 "find_appointment", "reschedule_appointment", "cancel_appointment", "end_call"):
        assert tool in prompt


def test_prompt_asks_for_a_time_instead_of_offering_two():
    """A day with no time: the agent asks what time, then checks that time."""
    prompt = build_prompt(business_name="Brightside Dental")
    assert "Ask what time they would like" in prompt
    assert "Never pick or suggest times yourself." in prompt
    assert "offer the two times" not in prompt
    assert "What time would you like on Wednesday?" in prompt


def test_prompt_is_the_same_for_every_call_and_model():
    a = build_prompt(business_name="Brightside Dental", model="openai")
    b = build_prompt(business_name="Brightside Dental", model="groq")
    assert a == b and "Today is" not in a


def test_prompt_does_not_script_the_recorded_call():
    """It is a working agent: the demo's own lines are not in its prompt."""
    prompt = build_prompt(business_name="Brightside Dental")
    assert "Alex Tan" not in prompt and "tomorrow at two" not in prompt.lower()


# ── a plain yes ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "Yes, please.", "yes", "Yeah, that works.", "Sure, go ahead.", "Okay.", "Please do.", "Sounds good, thanks.",
])
def test_plain_yes(text):
    assert is_plain_yes(text)


@pytest.mark.parametrize("text", [
    "", "Yes, but can we do three instead?", "Actually, sorry, I've got a meeting then.",
    "No thanks.", "Yes, and can you also cancel my other appointment on Monday please?",
    "That's all, thanks. Bye.", "What times do you have?", "Yesterday I called about this.",
    "Okay, not that one.",
])
def test_not_a_plain_yes(text):
    assert not is_plain_yes(text)


# ── which write a yes is for ────────────────────────────────────────────────


def test_yes_to_an_offered_time_requires_the_booking():
    deps = _offered()
    required = confirm_pending_write(deps, _ctx(_agent("Tomorrow at two is free. Shall I book it?"), _user("Yes, please.")))
    assert required == ({"type": "function", "function": {"name": "book_appointment"}}, None)


def test_yes_after_a_lookup_and_a_new_time_requires_the_move():
    deps = _offered(
        _appt_tokens={"appt_1": {"appointment": {"id": "a1", "status": "confirmed"}, "own": True}},
        _tool_call_log=[
            {"name": "check_slot", "success": True},
            {"name": "book_appointment", "success": True},
            {"name": "find_appointment", "success": True},
            {"name": "check_slot", "success": True},
        ],
    )
    required = confirm_pending_write(deps, _ctx(_user("Yes, please.")))
    assert required[0]["function"]["name"] == "reschedule_appointment"


def test_a_second_new_booking_on_the_same_call_is_a_booking_not_a_move():
    """An appointment exists from earlier in the call, but it was not looked
    up again: the caller is booking another one."""
    deps = _offered(
        _appt_tokens={"appt_1": {"appointment": {"id": "a1", "status": "confirmed"}, "own": True}},
        _tool_call_log=[
            {"name": "book_appointment", "success": True},
            {"name": "check_slot", "success": True},
        ],
    )
    assert pending_tool(deps) == "book_appointment"


def test_nothing_is_required_without_a_single_time_on_offer():
    yes = _ctx(_user("Yes, please."))
    assert confirm_pending_write({}, yes) is None
    # After a completed write the offer is cleared (common.mark_write).
    assert confirm_pending_write({"_slot_tokens": {"slot_a": {}}}, yes) is None
    # A stale token that is no longer registered.
    assert confirm_pending_write({"_slot_tokens": {}, "_last_offered_token": "slot_a"}, yes) is None


def test_nothing_is_required_when_the_caller_did_not_just_say_yes():
    deps = _offered()
    assert confirm_pending_write(deps, _ctx(_user("Actually, make it three."))) is None
    # The model's own follow-up after a tool returned: the last item is not the caller.
    tool_output = SimpleNamespace(type="function_call_output", output="STATE:slot_ok")
    assert confirm_pending_write(deps, _ctx(_user("Yes, please."), tool_output)) is None
    assert confirm_pending_write(deps, _ctx(_user("Yes."), _agent("Booked."))) is None
    assert confirm_pending_write(deps, _ctx()) is None


def test_the_shared_agent_applies_a_turn_policy_only_when_given_one():
    src = (_SRC / "agents" / "appointments" / "entry.py").read_text(encoding="utf-8")
    assert "if self._turn_policy is not None:" in src
    assert 'turn_policy=profile.get("turn_policy")' in src


# ── a move is never a second booking ────────────────────────────────────────


def _done(text):
    return SimpleNamespace(type="function_call_output", output=text)


def test_a_requested_move_is_a_move_even_when_the_appointment_was_not_looked_up():
    """The model checked the new time without calling find_appointment: the
    yes must still move the appointment booked earlier on the call."""
    deps = _offered(
        _appt_tokens={"appt_1": {"appointment": {"id": "a1", "status": "confirmed"}, "own": True}},
        _tool_call_log=[{"name": "book_appointment", "success": True}, {"name": "check_slot", "success": True}],
    )
    ctx = _ctx(
        _user("Can I book a cleaning for tomorrow at two?"),
        _done("BOOKED: confirm the day and time"),
        _agent("You're booked. Anything else?"),
        _user("Actually, sorry, I've got a meeting then. Could you move it to Friday at ten?"),
        _done("STATE:slot_ok token=slot_a"),
        _agent("Friday at ten is free. Shall I move it there?"),
        _user("Yes, please."),
    )
    assert confirm_pending_write(deps, ctx)[0]["function"]["name"] == "reschedule_appointment"


@pytest.mark.parametrize("ask", [
    "Can we reschedule to Friday at ten?", "I can't make it, how about Friday at ten?",
    "Could we do Friday at ten instead?", "I need to change my appointment to Friday at ten.",
])
def test_ways_of_asking_for_a_move(ask):
    deps = _offered(_appt_tokens={"appt_1": {"appointment": {"id": "a1", "status": "confirmed"}, "own": True}})
    ctx = _ctx(_done("BOOKED: ok"), _user(ask), _agent("Friday at ten is free. Shall I move it there?"), _user("Yes."))
    assert confirm_pending_write(deps, ctx)[0]["function"]["name"] == "reschedule_appointment"


def test_a_move_request_from_before_the_last_change_does_not_count():
    """The move already happened; a further time on offer is a new booking."""
    deps = _offered(
        _appt_tokens={"appt_1": {"appointment": {"id": "a1", "status": "confirmed"}, "own": True}},
        _tool_call_log=[{"name": "find_appointment", "success": True}, {"name": "reschedule_appointment", "success": True}],
    )
    ctx = _ctx(
        _user("Could you move it to Friday at ten?"),
        _done("RESCHEDULED: confirm the new day"),
        _user("Also, can I book a check-up for my son on Monday at nine?"),
        _agent("Monday at nine is free. Shall I book it?"),
        _user("Yes, please."),
    )
    assert confirm_pending_write(deps, ctx)[0]["function"]["name"] == "book_appointment"


def test_a_move_request_with_no_appointment_is_a_booking():
    ctx = _ctx(_user("Could we do Friday at ten instead?"), _agent("Friday at ten is free. Shall I book it?"), _user("Yes."))
    assert confirm_pending_write(_offered(), ctx)[0]["function"]["name"] == "book_appointment"


# ── register: respectful and professional ───────────────────────────────────


def test_prompt_asks_for_a_respectful_professional_register():
    prompt = build_prompt(business_name="Brightside Dental")
    assert "courteous, respectful and professional" in prompt
    assert "Be polite and respectful in every reply." in prompt
    assert "Would you like me to book it for you?" in prompt
    assert "Is there anything else I can help you with?" in prompt
    # The casual lines the first version taught by example are gone.
    for casual in ("Shall I book it?", "Done, you're now on", "You're booked for Monday at nine, Dana."):
        assert casual not in prompt


def test_the_fixed_lines_match_the_register_and_keep_the_recording_notice():
    assert PROFILE["greeting"].format(business_name="Brightside Dental") == (
        "Thank you for calling Brightside Dental. This call is recorded. How may I help you today?"
    )
    assert "Have a lovely day" in PROFILE["goodbye"]
    src = (_SRC / "agents" / "appointments" / "entry.py").read_text(encoding="utf-8")
    assert 'profile.get("greeting")' in src and 'profile.get("goodbye")' in src


# ── a day with no time: ask, never choose ───────────────────────────────────

import asyncio
import time as _time

from src.agents.appointments.tools.availability import (
    asked_what_is_free,
    check_day_impl,
    check_slot_impl,
    names_a_time,
    time_not_given,
)


def _said(*texts, **extra):
    deps = {"_ask_for_time": True, "_caller_texts": [(_time.time(), t) for t in texts]}
    deps.update(extra)
    return deps


@pytest.mark.parametrize("text", [
    "Can I book a cleaning for tomorrow at two?", "Three o'clock.", "3:30 please", "at 2PM",
    "Half past nine", "Could you move it to Friday at ten?", "Noon works", "At one, please",
])
def test_a_time_of_day_is_recognised(text):
    assert names_a_time(text)


@pytest.mark.parametrize("text", [
    "Can I book a cleaning for tomorrow?", "Monday the seventh of December", "On December 7",
    "The 4th of December, 2026", "Friday afternoon", "Can I book one for Tuesday?", "",
])
def test_a_day_or_a_date_is_not_a_time(text):
    assert not names_a_time(text)


def test_the_demo_profile_asks_for_a_time():
    assert PROFILE["ask_for_time"] is True


def test_check_slot_refuses_a_time_the_caller_never_said():
    deps = _said("Hi, it's Sam Wong. Can I book a check-up for tomorrow?")
    out = asyncio.run(check_slot_impl(deps, "2026-12-04", "08:00"))
    assert out.startswith("STATE:time_not_given") and "Ask what time" in out
    # Once per date: a time this test could not read costs one question only.
    assert time_not_given(deps, "2026-12-04") is False


def test_check_day_does_not_offer_times_unless_the_caller_asked_what_is_free():
    deps = _said("Can I book a check-up for tomorrow?")
    out = asyncio.run(check_day_impl(deps, "2026-12-04"))
    assert out.startswith("STATE:time_not_given") and "do not suggest times" in out
    asking = _said("What times do you have tomorrow?")
    assert asked_what_is_free(asking) and not asked_what_is_free(deps)


def test_a_time_the_caller_said_is_checked():
    assert time_not_given(_said("Can I book a cleaning for tomorrow at two?"), "2026-12-04") is False
    assert time_not_given(_said("Tomorrow please", "Three o'clock."), "2026-12-04") is False


def test_what_was_said_before_the_last_booking_change_does_not_count():
    deps = _said("Tomorrow at two, please.")
    deps["_last_write_at"] = _time.time() + 1
    deps["_caller_texts"].append((_time.time() + 2, "Could you move it to Friday?"))
    assert time_not_given(deps, "2026-11-27") is True


def test_other_agents_are_not_gated():
    deps = _said("Can I book something for tomorrow?")
    deps["_ask_for_time"] = False
    assert time_not_given(deps, "2026-12-04") is False
    assert time_not_given({"_ask_for_time": True, "_caller_texts": []}, "2026-12-04") is False


# -- what speech-to-text listens for ------------------------------------------

from src.agents.appointments.pipeline import resolve_stt_language


def test_the_demo_agent_listens_for_english_unless_told_otherwise():
    assert PROFILE["stt_language"] == "en"
    assert resolve_stt_language(None, PROFILE["stt_language"]) == "en"
    assert resolve_stt_language("multi", PROFILE["stt_language"]) == "multi"


def test_other_agents_keep_listening_for_every_language():
    assert resolve_stt_language(None, None) == "multi"
    assert resolve_stt_language("en", None) == "en"
    assert resolve_stt_language("klingon", None) == "multi"
    assert resolve_stt_language("klingon", "en") == "en"


def test_only_a_test_call_can_choose_the_language():
    source = (_SRC / "agents" / "appointments" / "entry.py").read_text(encoding="utf-8")
    assert 'room_meta.get("stt_language") if is_test_call else None' in source
