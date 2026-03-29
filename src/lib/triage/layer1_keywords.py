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


def run_keyword_classifier(transcript: str | None) -> dict:
    if not transcript or len(transcript) < 10:
        return {"result": "routine", "confident": False}

    # Check routine patterns first
    for pattern in ROUTINE_PATTERNS:
        if pattern.search(transcript):
            return {"result": "routine", "confident": True, "matched": pattern.pattern}

    for pattern in EMERGENCY_PATTERNS:
        if pattern.search(transcript):
            return {"result": "emergency", "confident": True, "matched": pattern.pattern}

    return {"result": "routine", "confident": False}
