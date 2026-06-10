import re

EMERGENCY_PATTERNS = [
    re.compile(r"\b(flooding|flooded|flood)\b", re.IGNORECASE),
    re.compile(r"\bgas\s*(smell|leak|line)\b", re.IGNORECASE),
    re.compile(r"\bno\s*(heat|hot\s*water)\b", re.IGNORECASE),
    re.compile(r"\bsewer\s*(backup|overflow)\b", re.IGNORECASE),
    re.compile(r"\bpipe\s*(burst|broke|broken)\b", re.IGNORECASE),
    re.compile(r"\belectrical\s*(fire|sparks?|smoke)\b", re.IGNORECASE),
    re.compile(r"\bcarbon\s*monoxide\b", re.IGNORECASE),
    re.compile(r"\b(right\s*now|happening\s*now|emergency|urgent)\b", re.IGNORECASE),
]

ROUTINE_PATTERNS = [
    re.compile(r"\b(quote|estimate|next\s*(week|month)|sometime|schedule)\b", re.IGNORECASE),
    re.compile(r"\b(not\s*urgent|whenever|no\s*rush)\b", re.IGNORECASE),
]

# Speaker prefixes as built by post_call.py:
#   f"{'Caller' if t['role'] == 'user' else 'AI'}: {t['content']}"
_CALLER_PREFIX = "Caller:"
_AI_PREFIX = "AI:"


def extract_caller_text(transcript: str | None) -> str:
    """Return only the caller's lines from a 'Caller:'/'AI:'-prefixed transcript.

    The agent's own speech must never drive classification — the prompt makes
    the agent say things like "let me take a look at the schedule", which would
    otherwise confidently match ROUTINE_PATTERNS and downgrade a real emergency.
    Falls back to the full text when the input has no speaker prefixes (raw
    text), and to an empty string when only AI lines are present.
    """
    if not transcript:
        return ""
    lines = transcript.splitlines()
    caller_lines = [line for line in lines if line.startswith(_CALLER_PREFIX)]
    if caller_lines:
        return "\n".join(caller_lines)
    if any(line.startswith(_AI_PREFIX) for line in lines):
        return ""  # agent-only transcript — nothing the caller said to classify
    return transcript


def run_keyword_classifier(transcript: str | None) -> dict:
    caller_text = extract_caller_text(transcript)
    if not caller_text or len(caller_text) < 10:
        return {"result": "routine", "confident": False}

    # Emergency patterns FIRST — an emergency match always wins over any
    # routine match ("gas leak, but no rush" is still an emergency).
    for pattern in EMERGENCY_PATTERNS:
        if pattern.search(caller_text):
            return {"result": "emergency", "confident": True, "matched": pattern.pattern}

    for pattern in ROUTINE_PATTERNS:
        if pattern.search(caller_text):
            return {"result": "routine", "confident": True, "matched": pattern.pattern}

    return {"result": "routine", "confident": False}
