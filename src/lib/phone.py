"""Phone number normalization helpers.

Previously a closure inside src/agent.py::entrypoint(). Extracted in Phase 39
so that src/webhook/twilio_routes.py can import the same normalization logic
(resolves RESEARCH.md OQ-3). Behavior is preserved verbatim.
"""
from __future__ import annotations


def _normalize_phone(number: str) -> str:
    """Normalize a LiveKit SIP phone attribute to E.164 format.

    Strips sip:/tel: prefixes, @domain suffixes, and prepends + if the
    remaining string starts with a digit. Returns the input unchanged if
    it's falsy (None or empty).

    Args:
        number: Raw SIP attribute value (e.g. "sip:+15551234567@domain.com")

    Returns:
        E.164-formatted number (e.g. "+15551234567") or the input unchanged
        if empty.
    """
    if not number:
        return number
    if number.lower().startswith("sip:"):
        number = number[4:]
    if "@" in number:
        number = number.split("@")[0]
    if number.lower().startswith("tel:"):
        number = number[4:]
    number = number.strip()
    if number and number[0].isdigit():
        number = "+" + number
    return number
