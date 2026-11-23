"""Scheduling profile and fast LLM path (shipped 2026-11-23, commit c1b048e).

Covers what had no tests: the short scheduling prompt's invariants, the
five-tool surface and the booking schema, and the LLM provider selection
(Groq first with gpt-4.1-mini as the fallback leg, scheduling profile only).

The provider block lives inside the agent entrypoint and cannot be run without
a LiveKit job, so its wiring is checked against the source, the same way the
other entrypoint invariants are (tests/test_test_call_sandbox.py).
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from src.prompt import build_scheduling_prompt, build_system_prompt
from src.tools import create_tools
from src.tools.book_appointment import (
    _BOOK_APPOINTMENT_SCHEDULING_SCHEMA,
    _BOOK_APPOINTMENT_SCHEMA,
)

_SRC = Path(__file__).parent.parent / "src"
AGENT_SRC = (_SRC / "agent.py").read_text(encoding="utf-8")

HOURS = {
    day: {"enabled": True, "open": "08:00", "close": "17:00"}
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
}


def _prompt(**kwargs) -> str:
    params = dict(
        business_name="Voco",
        tone_preset="professional",
        working_hours=HOURS,
        tenant_timezone="Asia/Singapore",
        locale="en",
    )
    params.update(kwargs)
    return build_scheduling_prompt(**params)


def _tool_name(tool) -> str:
    info = getattr(tool, "info", None)
    name = getattr(info, "name", None) or getattr(tool, "name", None)
    if name:
        return name
    raw = getattr(info, "raw_schema", None) or {}
    return raw.get("name") or getattr(tool, "__name__", "")


# ── prompt invariants ───────────────────────────────────────────────────────


def test_prompt_names_the_business():
    assert "You are the voice assistant for Voco." in _prompt()
    assert "Acme Dental" in _prompt(business_name="Acme Dental")


@pytest.mark.parametrize(
    "invariant",
    [
        # never invent availability
        "You know nothing about the calendar except what a tool returns",
        "Never state or guess a free time yourself",
        # read back and wait for a yes before booking
        "say the name and the day and time back once and wait for a clear yes",
        # never claim a booking before the tool says so
        "Only after book_appointment returns BOOKED may you say it is booked",
        # never book twice, never invent a token
        "Never book the same time twice",
        "Never invent a slot_token",
        # tool returns are instructions, not speech
        "never read a return aloud",
        # the runtime covers waits
        "Call tools directly, without announcing them",
        # same-turn goodbye
        "call end_call in the same turn",
    ],
)
def test_prompt_keeps_booking_invariants(invariant):
    assert invariant in " ".join(_prompt().split())


def test_prompt_routes_each_kind_of_request_to_a_tool():
    prompt = _prompt()
    for tool in ("check_slot", "check_day", "next_available_days", "book_appointment", "end_call"):
        assert tool in prompt


def test_prompt_has_no_home_service_intake():
    prompt = _prompt().lower()
    for term in ("address", "urgency", "emergency", "transfer", "capture_lead", "postal"):
        assert term not in prompt, f"scheduling prompt must not mention {term!r}"


def test_prompt_is_short():
    # The measured LLM first-token time grows with the prompt; the scheduling
    # prompt is deliberately a fraction of the home-service one.
    scheduling = _prompt()
    assert len(scheduling) < 6000
    home_service = build_system_prompt("en", business_name="Voco", onboarding_complete=True)
    assert len(scheduling) < len(home_service) / 3


def test_prompt_is_stable_across_calls():
    # Nothing time-dependent is rendered (agent.py appends "Today is ..."), so
    # the prompt prefix stays cacheable.
    assert _prompt() == _prompt()
    assert "Today is" not in _prompt()


def test_prompt_includes_working_hours():
    prompt = _prompt()
    assert "5:00 PM" in prompt
    assert "Sat-Sun: Closed" in prompt
    assert "Never guess or make up business hours" in prompt


# ── tool surface and booking schema ─────────────────────────────────────────


def test_scheduling_profile_registers_exactly_five_tools():
    tools = create_tools({"agent_profile": "scheduling", "onboarding_complete": True})
    assert [_tool_name(t) for t in tools] == [
        "check_slot",
        "check_day",
        "next_available_days",
        "book_appointment",
        "end_call",
    ]


def test_scheduling_tools_do_not_depend_on_onboarding_state():
    tools = create_tools({"agent_profile": "scheduling", "onboarding_complete": False})
    assert len(tools) == 5


def test_home_service_profile_keeps_its_tools():
    names = [
        _tool_name(t)
        for t in create_tools({"agent_profile": "home_service", "onboarding_complete": True})
    ]
    for tool in ("transfer_call", "capture_lead", "validate_address", "book_appointment"):
        assert tool in names


def test_scheduling_booking_schema_has_no_address_or_urgency():
    params = _BOOK_APPOINTMENT_SCHEDULING_SCHEMA["parameters"]
    assert set(params["properties"]) == {"slot_token", "caller_name", "reason"}
    assert params["required"] == ["slot_token", "caller_name"]
    assert _BOOK_APPOINTMENT_SCHEDULING_SCHEMA["name"] == "book_appointment"


def test_home_service_booking_schema_still_requires_the_address():
    required = _BOOK_APPOINTMENT_SCHEMA["parameters"]["required"]
    assert "street_name" in required and "postal_code" in required


# ── provider selection and fallback wiring ──────────────────────────────────


def test_defaults_are_scheduling_on_groq_qwen():
    assert 'os.environ.get("VOCO_AGENT_PROFILE", "scheduling")' in AGENT_SRC
    assert 'os.environ.get("VOCO_LLM_PROVIDER", "groq")' in AGENT_SRC
    assert 'os.environ.get("VOCO_GROQ_MODEL", "qwen/qwen3.8-27b")' in AGENT_SRC
    assert 'os.environ.get("VOCO_LLM_ATTEMPT_TIMEOUT_S", "4")' in AGENT_SRC


def test_groq_needs_provider_profile_and_key():
    # Groq is used only when the provider is groq AND the profile is scheduling
    # AND a key is present; anything else runs on OpenAI alone.
    assert (
        'if LLM_PROVIDER == "groq" and AGENT_PROFILE == "scheduling" and _groq_key:'
        in AGENT_SRC
    )
    assert '_groq_key = os.environ.get("GROQ_API_KEY", "").strip()' in AGENT_SRC


def test_fallback_order_is_groq_then_openai():
    block = AGENT_SRC[AGENT_SRC.index("_openai_llm = openai.LLM("):]
    block = block[: block.index("# Per-request LLM metrics")]
    assert "_agents_llm.FallbackAdapter(" in block
    assert "[_groq_llm, _openai_llm]," in block
    assert "attempt_timeout=LLM_FALLBACK_ATTEMPT_TIMEOUT_S," in block
    assert '_llm_legs = [("groq", _groq_llm), ("openai", _openai_llm)]' in block
    # OpenAI is the starting value, so it is what runs when Groq is not chosen.
    assert block.index("llm = _openai_llm") < block.index("FallbackAdapter(")


def test_groq_construction_failure_falls_back_to_openai_only():
    block = AGENT_SRC[AGENT_SRC.index("_groq_llm = openai.LLM("):]
    block = block[: block.index("# Per-request LLM metrics")]
    handler = block[block.index("except Exception as e:"):]
    assert "llm = _openai_llm" in handler
    assert '_llm_legs = [("openai", _openai_llm)]' in handler


def test_both_legs_keep_one_tool_call_at_a_time():
    assert AGENT_SRC.count("parallel_tool_calls=False") >= 2


def test_each_call_logs_which_llm_it_used():
    assert '"[agent] LLM: groq %s (reasoning=%s) -> fallback openai %s"' in AGENT_SRC
    assert '"[agent] LLM: openai %s (provider=%s profile=%s groq_key=%s)"' in AGENT_SRC


@pytest.mark.parametrize(
    "env, attr, expected",
    [
        ({}, "AGENT_PROFILE", "scheduling"),
        ({}, "LLM_PROVIDER", "groq"),
        ({}, "GROQ_LLM_MODEL", "qwen/qwen3.8-27b"),
        ({"VOCO_AGENT_PROFILE": " Home_Service "}, "AGENT_PROFILE", "home_service"),
        ({"VOCO_LLM_PROVIDER": "OpenAI"}, "LLM_PROVIDER", "openai"),
        ({"VOCO_GROQ_MODEL": "openai/gpt-oss-120b"}, "GROQ_LLM_MODEL", "openai/gpt-oss-120b"),
        ({"VOCO_VAD_MIN_SILENCE_S": "0.55"}, "VAD_MIN_SILENCE_S", 0.55),
    ],
)
def test_env_switches(monkeypatch, env, attr, expected):
    for name in (
        "VOCO_AGENT_PROFILE", "VOCO_LLM_PROVIDER", "VOCO_GROQ_MODEL", "VOCO_VAD_MIN_SILENCE_S",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    import src.agent as agent

    try:
        assert getattr(importlib.reload(agent), attr) == expected
    finally:
        monkeypatch.undo()
        importlib.reload(agent)
