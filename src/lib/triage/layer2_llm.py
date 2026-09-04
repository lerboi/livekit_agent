import asyncio
import json
import logging
import os
import time

import httpx
from openai import AsyncOpenAI, DefaultAsyncHttpxClient

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None

# 2026-09-04 (P1.1): layer 2 moved OFF Groq to OpenAI. Groq shut down
# `llama-4-scout-17b-16e-instruct` on 2026-07-17 and both `llama-3.3-70b-versatile`
# and `llama-3.1-8b-instant` on 2026-08-16; its remaining LLMs (gpt-oss-120b/20b)
# are reasoning models whose thinking tokens eat a 100-token budget → empty
# content → JSON error → the blanket fallback below silently reported every
# unclear call as routine/low from mid-July on. gpt-4.1-nano is current,
# non-reasoning, supports json_object mode, and uses OPENAI_API_KEY — which the
# `__main__` boot preflight already requires, so the missing-key gap closes by
# construction. ~1–2k input tokens per classification → well under $0.001/call.
LAYER2_MODEL = "gpt-4.1-nano"

SYSTEM_PROMPT = (
    'You classify home service calls. Return ONLY a JSON object: '
    '{"urgency": "emergency"|"routine"|"urgent", '
    '"confidence": "high"|"medium"|"low", "reason": "one sentence"}\n'
    "Emergency: immediate safety risk, happening right now, property damage ongoing.\n"
    "Urgent: needs prompt attention but not an immediate safety risk — e.g., broken AC in summer, "
    "clogged drain, water heater out, no hot water.\n"
    "Routine: future scheduling, quote requests, non-urgent repairs."
)

# 2.5s, not 5.0 (2026-06-12 audit H8): the whole post-call pipeline runs under
# an 8s wait_for (itself capped by the SDK's 10s shutdown budget). A 5s Layer-2
# call plus normal DB latency starved §6.5 record_outcome and §7 owner
# notifications — on slow calls the inquiry row was never created and EMERGENCY
# alerts never sent (the 2026-04-21 incident class). Layer 1 already
# short-circuits confident classifications, and a Layer-2 timeout falls back to
# the Layer-1 verdict, so tightening this trades marginal triage precision on
# slow calls for guaranteed delivery of the outcome + notification writes.
# 2026-09-04: env-overridable (VOCO_TRIAGE_LAYER2_TIMEOUT_S) so it can be tuned
# from Railway if the `[triage] layer2 timeout` line shows up in the logs.
TIMEOUT_S = float(os.environ.get("VOCO_TRIAGE_LAYER2_TIMEOUT_S", "2.5"))

# Measured 2026-09-04: gpt-4.1-nano answers this prompt in ~0.85 s on a warm
# connection, but the FIRST request in a process also pays the TLS handshake to
# api.openai.com (~1.2 s from Singapore, ~0.2 s from a US worker) — and every
# call's post-call triage runs in a fresh job process, so it is always the
# cold case. `warm_client()` (fired in the background at call start from
# agent.py, gated by VOCO_TRIAGE_LAYER2_WARM) opens the connection while the
# call is in progress; the keep-alive expiry is disabled so it survives until
# the post-call pipeline needs it.
WARM_TIMEOUT_S = 4.0

_FALLBACK = {"urgency": "routine", "confidence": "low", "reason": "timeout or error"}


def _get_client() -> AsyncOpenAI:
    """Lazily built module global. No base_url / api_key: the SDK reads
    OPENAI_API_KEY from the environment (the same key the voice LLM uses).
    The SDK's own default httpx client class is used (same timeouts /
    redirects) with keep-alive expiry disabled so a warmed connection is still
    open when the post-call pipeline runs."""
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            http_client=DefaultAsyncHttpxClient(
                limits=httpx.Limits(
                    max_connections=4,
                    max_keepalive_connections=2,
                    keepalive_expiry=None,
                ),
            ),
        )
    return _client


async def warm_client(timeout_s: float = WARM_TIMEOUT_S) -> bool:
    """Open the TLS session to api.openai.com ahead of the post-call
    classification (one tiny GET). Never raises."""
    try:
        await asyncio.wait_for(_get_client().models.retrieve(LAYER2_MODEL), timeout=timeout_s)
        return True
    except Exception as exc:  # noqa: BLE001 — background warm must never surface
        logger.debug("[triage] layer2 connection warm failed: %s", exc)
        return False


async def run_llm_scorer(transcript: str) -> dict:
    t0 = time.perf_counter()
    try:
        response = await asyncio.wait_for(
            _get_client().chat.completions.create(
                model=LAYER2_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"Call transcript:\n{transcript}"},
                ],
                response_format={"type": "json_object"},
                max_tokens=100,
                temperature=0,
            ),
            timeout=TIMEOUT_S,
        )
        result = json.loads(response.choices[0].message.content)
        logger.info(
            "[triage] layer2 ok urgency=%s confidence=%s elapsed_ms=%d",
            result.get("urgency"), result.get("confidence"),
            int((time.perf_counter() - t0) * 1000),
        )
        return result
    except asyncio.TimeoutError:
        # Post-call pipeline must never raise; the caller keeps the layer-1 verdict.
        logger.warning("[triage] layer2 timeout after %.1fs", TIMEOUT_S)
        return dict(_FALLBACK)
    except Exception as e:
        logger.error(
            "[triage] layer2 failed after %dms: %s",
            int((time.perf_counter() - t0) * 1000), e,
        )
        return dict(_FALLBACK)
