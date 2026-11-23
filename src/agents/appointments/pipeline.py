"""The voice stack for the appointments agent: STT, turn detection, LLM.

Every tuning value comes from ``cfg``, which is the already-loaded
``src/agent.py`` module handed over by the router. Both agents therefore read
the same constants and the same env switches (``VOCO_LLM_PROVIDER``,
``VOCO_GROQ_MODEL``, ``VOCO_VAD_MIN_SILENCE_S``, the endpointing and
interruption settings, ...): there is one place to tune the voice, and no
second copy to drift.
"""

from __future__ import annotations

import logging
import os

from livekit.agents import inference
from livekit.agents import llm as agents_llm
from livekit.plugins import deepgram, openai
from livekit.plugins.turn_detector.multilingual import MultilingualModel

logger = logging.getLogger("voco-agent")


def build_stt(cfg, keyterms: list[str]):
    kwargs = {}
    terms = [t for t in keyterms if t][:20]
    if cfg.STT_KEYTERMS_ENABLED and terms:
        kwargs["keyterm"] = terms
    return deepgram.STT(model="nova-3", language="multi", **kwargs)


def build_turn_detector(cfg):
    """Audio turn detector, falling back to the text model so a call is never
    left without one."""
    if cfg.TURN_DETECTOR_MODE == "text":
        return MultilingualModel()
    try:
        kwargs = {"version": cfg.TURN_DETECTOR_VERSION}
        if cfg.TURN_DETECTOR_UNLIKELY_THRESHOLD is not None:
            kwargs["unlikely_threshold"] = cfg.TURN_DETECTOR_UNLIKELY_THRESHOLD
        return inference.TurnDetector(**kwargs)
    except Exception as err:  # noqa: BLE001
        logger.error("[appointments] audio turn detector unavailable (%s); using the text model", err)
        return MultilingualModel()


PROVIDERS = ("groq", "openai")


def resolve_provider(cfg, requested: str | None = None) -> str:
    """Which model family this call runs on: "groq" (Groq Qwen first,
    gpt-4.1-mini as fallback) or "openai" (gpt-4.1-mini alone).

    In order: ``requested`` (the Demo Studio's choice, passed in by the
    entrypoint for test calls only), then ``VOCO_APPT_LLM_PROVIDER`` (this
    agent's production setting), then the shared ``VOCO_LLM_PROVIDER``.
    Anything unrecognised is skipped. "groq" without a GROQ_API_KEY runs as
    "openai".
    """
    candidates = (
        requested,
        os.environ.get("VOCO_APPT_LLM_PROVIDER"),
        cfg.LLM_PROVIDER,
    )
    provider = next(
        (str(c).strip().lower() for c in candidates if str(c or "").strip().lower() in PROVIDERS),
        "openai",
    )
    if provider == "groq" and not os.environ.get("GROQ_API_KEY", "").strip():
        logger.warning("[appointments] groq asked for but GROQ_API_KEY is not set; using openai")
        return "openai"
    return provider


def build_llm(cfg, tenant_id, provider: str = "groq"):
    """(llm, legs) for a provider from ``resolve_provider``. Groq first with
    gpt-4.1-mini as the fallback leg, or gpt-4.1-mini alone. ``legs`` is
    [(provider, llm)] for metrics and cache warming, which must address each
    model directly.

    parallel_tool_calls=False: the token contract assumes one tool call
    resolves before the next (never check_slot and book in one step).
    """
    openai_kwargs = dict(model=cfg.LLM_MODEL, parallel_tool_calls=False, max_completion_tokens=500)
    cache_key = cfg._prompt_cache_key(tenant_id)
    if cache_key:
        openai_kwargs["prompt_cache_key"] = cache_key
    openai_llm = openai.LLM(**openai_kwargs)
    legs = [("openai", openai_llm)]

    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    if provider != "groq" or not groq_key:
        logger.info("[appointments] LLM: openai %s", cfg.LLM_MODEL)
        return openai_llm, legs
    try:
        groq_kwargs = dict(
            model=cfg.GROQ_LLM_MODEL,
            base_url=cfg.GROQ_BASE_URL,
            api_key=groq_key,
            parallel_tool_calls=False,
            max_completion_tokens=500,
        )
        if cfg.GROQ_REASONING_EFFORT and cfg.GROQ_REASONING_EFFORT != "off":
            groq_kwargs["reasoning_effort"] = cfg.GROQ_REASONING_EFFORT
        groq_llm = openai.LLM(**groq_kwargs)
        adapter = agents_llm.FallbackAdapter(
            [groq_llm, openai_llm], attempt_timeout=cfg.LLM_FALLBACK_ATTEMPT_TIMEOUT_S,
        )
        logger.info(
            "[appointments] LLM: groq %s (reasoning=%s) -> fallback openai %s",
            cfg.GROQ_LLM_MODEL, cfg.GROQ_REASONING_EFFORT, cfg.LLM_MODEL,
        )
        return adapter, [("groq", groq_llm), ("openai", openai_llm)]
    except Exception as err:  # noqa: BLE001
        logger.error("[appointments] Groq LLM unavailable (%s); using OpenAI only", err)
        return openai_llm, legs


def build_turn_handling(cfg, turn_detection) -> dict:
    return {
        "turn_detection": turn_detection,
        "endpointing": {
            "mode": cfg.ENDPOINTING_MODE,
            "min_delay": cfg.MIN_ENDPOINTING_DELAY_S,
            "max_delay": cfg.MAX_ENDPOINTING_DELAY_S,
        },
        "interruption": {
            "enabled": True,
            "mode": cfg.INTERRUPTION_MODE,
            "min_words": cfg.MIN_INTERRUPTION_WORDS,
            "min_duration": cfg.MIN_INTERRUPTION_DURATION_S,
            "resume_false_interruption": True,
            "false_interruption_timeout": cfg.FALSE_INTERRUPTION_TIMEOUT_S,
        },
        "preemptive_generation": {
            "enabled": cfg.PREEMPTIVE_GENERATION,
            "preemptive_tts": cfg.PREEMPTIVE_TTS,
        },
    }
