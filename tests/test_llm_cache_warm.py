"""2026-09-01 prompt caching — _warm_prompt_cache + _prompt_cache_key (src/agent.py).

The warm request must (1) call llm.chat exactly once with the session's own
chat_ctx/tools and a no-retry, bounded-timeout connect option, (2) read only
the FIRST streamed chunk then close the stream (the cache write happens at
prefill, before any token streams — draining the reply would just burn output
tokens), and (3) never raise — it runs as a fire-and-forget background task
during the greeting, and a failure must not touch the call.
"""
from __future__ import annotations

from src.agent import _prompt_cache_key, _warm_prompt_cache


class _FakeStream:
    def __init__(self, chunks=("a", "b", "c"), raise_on_iter: bool = False):
        self._chunks = list(chunks)
        self._raise = raise_on_iter
        self.consumed = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._raise:
            raise RuntimeError("boom mid-stream")
        if not self._chunks:
            raise StopAsyncIteration
        self.consumed += 1
        return self._chunks.pop(0)


class _FakeLLM:
    def __init__(self, stream=None, raise_on_chat: bool = False):
        self.stream = stream or _FakeStream()
        self.raise_on_chat = raise_on_chat
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_on_chat:
            raise RuntimeError("no network")
        return self.stream


def test_prompt_cache_key_is_per_tenant_and_stable():
    assert _prompt_cache_key("t-123") == "voco-agent:t-123"
    assert _prompt_cache_key("t-123") == _prompt_cache_key("t-123")
    assert _prompt_cache_key("t-123") != _prompt_cache_key("t-456")


def test_prompt_cache_key_none_without_tenant():
    assert _prompt_cache_key(None) is None
    assert _prompt_cache_key("") is None


async def test_warm_sends_exact_ctx_and_tools_reads_one_chunk_then_closes():
    llm = _FakeLLM()
    ctx, tools = object(), [object(), object()]
    await _warm_prompt_cache(llm, ctx, tools, timeout_s=6.0, call_id="c1")
    assert len(llm.calls) == 1
    kw = llm.calls[0]
    assert kw["chat_ctx"] is ctx
    assert kw["tools"] is tools
    assert kw["conn_options"].max_retry == 0
    assert kw["conn_options"].timeout == 6.0
    assert llm.stream.consumed == 1  # first chunk only
    assert llm.stream.closed is True


async def test_warm_swallows_chat_exception():
    llm = _FakeLLM(raise_on_chat=True)
    await _warm_prompt_cache(llm, object(), [], timeout_s=1.0)  # must not raise
    assert len(llm.calls) == 1


async def test_warm_swallows_mid_stream_exception_and_still_closes():
    llm = _FakeLLM(stream=_FakeStream(raise_on_iter=True))
    await _warm_prompt_cache(llm, object(), [], timeout_s=1.0)
    assert llm.stream.closed is True


async def test_warm_handles_empty_stream():
    llm = _FakeLLM(stream=_FakeStream(chunks=()))
    await _warm_prompt_cache(llm, object(), [], timeout_s=1.0)
    assert llm.stream.consumed == 0
    assert llm.stream.closed is True
