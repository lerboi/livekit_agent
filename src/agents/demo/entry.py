"""One demo call. The call itself is run by the appointments agent's
``run_call`` (same session wiring, tools, guards, recording and post-call);
this module supplies what is specific to the demo agent."""

from __future__ import annotations

from ..appointments.entry import run_call as run_appointments_call
from .policy import confirm_pending_write
from .prompt import SERVICES, build_prompt

AGENT_NAME = "demo"

PROFILE = {
    "agent_name": AGENT_NAME,
    "build_prompt": build_prompt,
    "turn_policy": confirm_pending_write,
    # Used when the Demo Studio's Model switch names none: the slower, steadier
    # model. Production rehearsals on 2026-11-24: gpt-4.1-mini 4 of 5 (the miss
    # was a misheard name), Groq Qwen 3 of 5 with replies no recording could use.
    "llm": "openai",
    # The fixed lines, in the same register as the prompt. The greeting still
    # says the call is recorded.
    "greeting": "Thank you for calling {business_name}. This call is recorded. How may I help you today?",
    "goodbye": "Thank you for calling {business_name}. Have a lovely day. Goodbye.",
    # Extra speech-to-text keyterms next to the business name.
    "keyterms": list(SERVICES) + ["appointment"],
}


async def run_call(ctx, base, **call) -> None:
    await run_appointments_call(ctx, base, profile=PROFILE, **call)
