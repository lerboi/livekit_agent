import asyncio
import json
import os

from openai import AsyncOpenAI

_client: AsyncOpenAI | None = None

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
TIMEOUT_S = 2.5


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=os.environ.get("GROQ_API_KEY", ""),
            base_url="https://api.groq.com/openai/v1",
        )
    return _client


async def run_llm_scorer(transcript: str) -> dict:
    try:
        response = await asyncio.wait_for(
            _get_client().chat.completions.create(
                model="meta-llama/llama-4-scout-17b-16e-instruct",
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
        return json.loads(response.choices[0].message.content)
    except Exception:
        return {"urgency": "routine", "confidence": "low", "reason": "timeout or error"}
