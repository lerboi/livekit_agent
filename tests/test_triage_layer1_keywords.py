"""Tests for src.lib.triage.layer1_keywords + the caller-only classify_call input.

Covers the emergency-downgrade bug: run_keyword_classifier used to check
ROUTINE_PATTERNS first with a confident short-circuit AND received the full
transcript including the agent's turns — the agent saying "let me take a look
at the schedule" matched \\bschedule\\b and confidently classified a gas-leak
call as routine. Fixed by (a) evaluating EMERGENCY_PATTERNS first and
(b) classifying on caller-only lines.
"""
from types import SimpleNamespace

import pytest

from src.lib.triage import classifier
from src.lib.triage.layer1_keywords import extract_caller_text, run_keyword_classifier


# ── extract_caller_text ──────────────────────────────────────────────────────

def test_extract_caller_text_filters_ai_lines():
    transcript = (
        "Caller: I think I have a gas leak in my kitchen\n"
        "AI: Let me take a look at the schedule for you\n"
        "Caller: please hurry"
    )
    assert extract_caller_text(transcript) == (
        "Caller: I think I have a gas leak in my kitchen\nCaller: please hurry"
    )


def test_extract_caller_text_ai_only_returns_empty():
    transcript = "AI: Hello, this is an emergency hotline, how can I help?"
    assert extract_caller_text(transcript) == ""


def test_extract_caller_text_raw_text_passthrough():
    raw = "my basement is flooding right now"
    assert extract_caller_text(raw) == raw


def test_extract_caller_text_none_and_empty():
    assert extract_caller_text(None) == ""
    assert extract_caller_text("") == ""


# ── run_keyword_classifier: emergency-first + caller-only ────────────────────

def test_gas_leak_not_downgraded_by_agent_schedule_speech():
    """THE bug: agent's 'schedule' line must not confidently mark a gas leak routine."""
    transcript = (
        "Caller: hi, I smell gas in my house, I think there's a gas leak\n"
        "AI: I'm so sorry to hear that. Let me take a look at the schedule\n"
        "Caller: okay thank you"
    )
    result = run_keyword_classifier(transcript)
    assert result["result"] == "emergency"
    assert result["confident"] is True


def test_emergency_wins_over_routine_in_same_caller_text():
    """Emergency patterns are evaluated FIRST — an emergency match beats routine."""
    transcript = (
        "Caller: my basement is flooding, but honestly no rush if you're busy"
    )
    result = run_keyword_classifier(transcript)
    assert result["result"] == "emergency"
    assert result["confident"] is True


def test_routine_caller_text_still_confidently_routine():
    transcript = (
        "Caller: I'd like to get a quote for a new water heater sometime next week\n"
        "AI: Sure, I can help with that"
    )
    result = run_keyword_classifier(transcript)
    assert result["result"] == "routine"
    assert result["confident"] is True


def test_emergency_keyword_only_in_ai_lines_not_confident():
    """Agent speech alone must not produce a confident classification."""
    transcript = (
        "AI: If this is an emergency like flooding, please tell me right away\n"
        "Caller: no nothing like that"
    )
    result = run_keyword_classifier(transcript)
    assert result["confident"] is False
    assert result["result"] == "routine"


def test_raw_unprefixed_text_still_classified():
    result = run_keyword_classifier("there is water flooding my kitchen floor")
    assert result["result"] == "emergency"
    assert result["confident"] is True


def test_short_or_empty_input_not_confident():
    assert run_keyword_classifier(None) == {"result": "routine", "confident": False}
    assert run_keyword_classifier("Caller: ok")["confident"] is False


# ── classify_call: layer2 receives caller-only text ──────────────────────────

class _FakeQuery:
    def __init__(self, data):
        self._data = data

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def execute(self):
        return SimpleNamespace(data=self._data)


class _FakeSupabase:
    def table(self, _name):
        return _FakeQuery([])  # no owner services — layer3 never escalates


@pytest.mark.asyncio
async def test_classify_call_emergency_confident_via_layer1():
    transcript = (
        "Caller: my pipe burst and water is everywhere\n"
        "AI: let me check the schedule for the next opening"
    )
    result = await classifier.classify_call(
        _FakeSupabase(), transcript=transcript, tenant_id="t1"
    )
    assert result["urgency"] == "emergency"
    assert result["confidence"] == "high"


@pytest.mark.asyncio
async def test_classify_call_passes_caller_only_text_to_layer2(monkeypatch):
    captured = {}

    async def _fake_llm_scorer(text):
        captured["text"] = text
        return {"urgency": "routine", "confidence": "low", "reason": "test"}

    monkeypatch.setattr(classifier, "run_llm_scorer", _fake_llm_scorer)

    transcript = (
        "Caller: hi I have a question about my water bill\n"
        "AI: of course, let me take a look at the schedule for you"
    )
    # No layer1 keyword match in the caller line -> falls through to layer2.
    await classifier.classify_call(
        _FakeSupabase(), transcript=transcript, tenant_id="t1"
    )
    assert "text" in captured
    assert "schedule" not in captured["text"]
    assert "water bill" in captured["text"]
