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
