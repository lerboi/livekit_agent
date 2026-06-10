"""Phone number normalization helpers.

Previously a closure inside src/agent.py::entrypoint(). Extracted in Phase 39
so that src/webhook/twilio_routes.py can import the same normalization logic
(resolves RESEARCH.md OQ-3). Behavior is preserved verbatim.

`derive_caller_region` (2026-06-10) lives here for the same reason: agent.py
uses it when building tool deps, and tests can import it without dragging in
the full agent module.
"""
from __future__ import annotations

from typing import Optional


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


def derive_caller_region(number: Optional[str]) -> Optional[str]:
    """Derive the caller's ISO 3166-1 alpha-2 region from an E.164 caller-ID.

    Used by agent.py to set deps["caller_region"], which feeds the
    address-validation region fallback (google_maps.
    validate_address_with_region_fallback). `phonenumbers` correctly
    disambiguates +1 US vs CA by area code (e.g. +1604... → "CA").

    Pure synchronous parsing — no I/O. NEVER raises: anonymous/withheld
    caller-ID, empty/None input, garbage strings, or any parser exception
    all return None (a missing caller region simply disables the fallback;
    it must never break session startup).

    Args:
        number: E.164 caller-ID (e.g. "+16045551234"), already normalized
                by _normalize_phone. May be None/empty/garbage.

    Returns:
        Region code string (e.g. "US", "CA", "SG") or None.
    """
    if not number:
        return None
    try:
        import phonenumbers

        parsed = phonenumbers.parse(number, None)
        # region_code_for_number returns None for unassignable numbers
        # (e.g. +1 with a non-existent area code) — passed through as-is.
        return phonenumbers.region_code_for_number(parsed)
    except Exception:  # noqa: BLE001 — must never break session startup
        return None
