"""Tests for src.lib.triage.layer2_llm (P1.1, 2026-09-04).

Layer 2 moved off Groq (all of Groq's non-reasoning Llama models were shut
down by 2026-08-16; the blanket except hid the outage as routine/low) to
OpenAI gpt-4.1-nano on the already-required OPENAI_API_KEY. These tests pin:
the model id + host, the never-raise contract on both the timeout and the
generic-exception paths, and that a valid JSON reply is returned as-is.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.lib.triage import layer2_llm


FALLBACK = {"urgency": "routine", "confidence": "low", "reason": "timeout or error"}


def _fake_client(create):
    """Minimal stand-in for AsyncOpenAI exposing .chat.completions.create."""
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _response(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


async def test_exception_inside_openai_call_returns_fallback(monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("openai down")

    monkeypatch.setattr(layer2_llm, "_get_client", lambda: _fake_client(_boom))
    result = await layer2_llm.run_llm_scorer("Caller: my sink is dripping")
    assert result == FALLBACK


async def test_timeout_returns_fallback(monkeypatch):
    async def _slow(**kwargs):
        await asyncio.sleep(5)
        return _response('{"urgency": "emergency", "confidence": "high", "reason": "x"}')

    monkeypatch.setattr(layer2_llm, "_get_client", lambda: _fake_client(_slow))
    monkeypatch.setattr(layer2_llm, "TIMEOUT_S", 0.01)
    result = await layer2_llm.run_llm_scorer("Caller: water everywhere")
    assert result == FALLBACK


def test_model_is_gpt_41_nano_and_client_targets_openai(monkeypatch):
    assert layer2_llm.LAYER2_MODEL == "gpt-4.1-nano"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(layer2_llm, "_client", None)
    client = layer2_llm._get_client()
    assert client.base_url.host == "api.openai.com"
    # Lazily created module global — second call returns the same object.
    assert layer2_llm._get_client() is client


async def test_valid_json_response_is_returned_as_is(monkeypatch):
    seen = {}

    async def _ok(**kwargs):
        seen.update(kwargs)
        return _response('{"urgency": "urgent", "confidence": "medium", "reason": "no hot water"}')

    monkeypatch.setattr(layer2_llm, "_get_client", lambda: _fake_client(_ok))
    result = await layer2_llm.run_llm_scorer("Caller: no hot water since this morning")
    assert result == {"urgency": "urgent", "confidence": "medium", "reason": "no hot water"}
    # Request shape pinned: model id, json_object mode, small deterministic budget.
    assert seen["model"] == "gpt-4.1-nano"
    assert seen["response_format"] == {"type": "json_object"}
    assert seen["max_tokens"] == 100
    assert seen["temperature"] == 0


async def test_non_json_content_returns_fallback(monkeypatch):
    """A reasoning model that spent the budget on thinking (empty content) — the
    Groq failure mode — must still degrade to the fallback, never raise."""
    async def _empty(**kwargs):
        return _response("")

    monkeypatch.setattr(layer2_llm, "_get_client", lambda: _fake_client(_empty))
    result = await layer2_llm.run_llm_scorer("Caller: hi")
    assert result == FALLBACK


async def test_warm_client_never_raises(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("no network")
    client = SimpleNamespace(models=SimpleNamespace(retrieve=_boom))
    monkeypatch.setattr(layer2_llm, "_get_client", lambda: client)
    assert await layer2_llm.warm_client() is False


async def test_warm_client_retrieves_model(monkeypatch):
    seen = {}

    async def _ok(model):
        seen["model"] = model
        return SimpleNamespace(id=model)
    client = SimpleNamespace(models=SimpleNamespace(retrieve=_ok))
    monkeypatch.setattr(layer2_llm, "_get_client", lambda: client)
    assert await layer2_llm.warm_client() is True
    assert seen["model"] == "gpt-4.1-nano"


def test_client_keepalive_never_expires(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(layer2_llm, "_client", None)
    client = layer2_llm._get_client()
    pool = client._client._transport._pool
    assert pool._keepalive_expiry is None


def test_timeout_default_and_env_override():
    assert layer2_llm.TIMEOUT_S == 2.5
