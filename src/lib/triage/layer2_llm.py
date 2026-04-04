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

TIMEOUT_S = 5.0


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
