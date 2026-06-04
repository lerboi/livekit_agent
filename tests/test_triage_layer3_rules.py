"""Tests for src.lib.triage.layer3_rules.apply_owner_rules (FIX H3).

Verifies the transcript-driven service-name match that makes layer3 actually
fire (classify_call never passes detected_service). Covers escalation on a
higher-urgency service name, the no-escalate-below-base property, the no-match
base path, the removed single-service auto-adopt regression, and the
short-name guard (MIN_SERVICE_NAME_LEN).
"""
from types import SimpleNamespace

import pytest

from src.lib.triage import layer3_rules
from src.lib.triage.layer3_rules import MIN_SERVICE_NAME_LEN, apply_owner_rules


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
    def __init__(self, services):
        self._services = services

    def table(self, _name):
        return _FakeQuery(self._services)


@pytest.mark.asyncio
async def test_service_name_in_transcript_with_higher_tag_escalates():
    services = [
        {"name": "boiler repair", "urgency_tag": "emergency"},
        {"name": "drain cleaning", "urgency_tag": "routine"},
    ]
    sb = _FakeSupabase(services)
    result = await apply_owner_rules(
        sb,
        base_urgency="routine",
        tenant_id="t1",
        transcript="Hi, my boiler repair is needed, the boiler repair stopped working.",
    )
    assert result["escalated"] is True
    assert result["urgency"] == "emergency"


@pytest.mark.asyncio
async def test_service_mentioned_but_tag_not_higher_does_not_escalate():
    services = [{"name": "drain cleaning", "urgency_tag": "routine"}]
    sb = _FakeSupabase(services)
    result = await apply_owner_rules(
        sb,
        base_urgency="urgent",  # already above the matched tag
        tenant_id="t1",
        transcript="I need a drain cleaning sometime next week.",
    )
    assert result["escalated"] is False
    assert result["urgency"] == "urgent"


@pytest.mark.asyncio
async def test_no_service_name_in_transcript_returns_base():
    services = [{"name": "boiler repair", "urgency_tag": "emergency"}]
    sb = _FakeSupabase(services)
    result = await apply_owner_rules(
        sb,
        base_urgency="routine",
        tenant_id="t1",
        transcript="Just calling to ask about your opening hours, thanks.",
    )
    assert result["escalated"] is False
    assert result["urgency"] == "routine"


@pytest.mark.asyncio
async def test_single_service_tenant_routine_unrelated_not_escalated():
    """Regression guard: removed auto-adopt must NOT escalate an unrelated call."""
    services = [{"name": "boiler repair", "urgency_tag": "emergency"}]
    sb = _FakeSupabase(services)
    result = await apply_owner_rules(
        sb,
        base_urgency="routine",
        tenant_id="t1",
        transcript="Hello, do you give free quotes for general work?",
    )
    assert result["escalated"] is False
    assert result["urgency"] == "routine"


@pytest.mark.asyncio
async def test_too_short_service_name_not_matched():
    """Service name shorter than MIN_SERVICE_NAME_LEN must be skipped."""
    short_name = "ac"
    assert len(short_name) < MIN_SERVICE_NAME_LEN
    services = [{"name": short_name, "urgency_tag": "emergency"}]
    sb = _FakeSupabase(services)
    result = await apply_owner_rules(
        sb,
        base_urgency="routine",
        tenant_id="t1",
        transcript="The ac unit is broken and needs urgent help.",
    )
    assert result["escalated"] is False
    assert result["urgency"] == "routine"


@pytest.mark.asyncio
async def test_word_boundary_prevents_substring_false_match():
    """'gas' must not match inside 'gasket' — word-boundary regex, not 'in'."""
    services = [{"name": "spag", "urgency_tag": "emergency"}]
    sb = _FakeSupabase(services)
    result = await apply_owner_rules(
        sb,
        base_urgency="routine",
        tenant_id="t1",
        transcript="I would like to order some spaghetti supplies.",
    )
    assert result["escalated"] is False
    assert result["urgency"] == "routine"


@pytest.mark.asyncio
async def test_min_service_name_len_constant_value():
    assert layer3_rules.MIN_SERVICE_NAME_LEN == 4
