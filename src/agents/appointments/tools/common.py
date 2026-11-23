"""Helpers shared by the appointments agent's tools.

Tool returns are ``STATE:... | DIRECTIVE:...`` strings (or ``BOOKED:`` /
``RESCHEDULED:`` / ``CANCELLED:`` for a completed write). The model acts on
the directive; it never reads a return aloud.
"""

from __future__ import annotations

import os
import secrets
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from ....tools import _availability_lib as lib
from ....utils import format_slot_for_speech

TOKEN_TTL_S = lib.SLOT_TOKEN_TTL_S

CONFIRM_GATE_ENABLED = os.environ.get("VOCO_CONFIRM_GATE", "true").strip().lower() not in (
    "0", "false", "off", "no",
)

# Seconds a tool may run before a spoken filler covers the wait. Measured on a
# scripted call (2026-11-24): with the shared 0.6 s default the booking filler
# ("Okay, booking that in for you") started as the booking finished, and the
# caller waited about four more seconds for the confirmation behind it. The
# typing sound covers the wait up to these points.
LOOKUP_FILLER_DELAY_S = float(os.environ.get("VOCO_APPT_LOOKUP_FILLER_DELAY_S", "1.2"))
WRITE_FILLER_DELAY_S = float(os.environ.get("VOCO_APPT_WRITE_FILLER_DELAY_S", "2.0"))

# Said when a lookup or write fails for a technical reason. This agent has no
# lead-capture or transfer tool, so the honest answer is "try again shortly".
TRY_AGAIN = (
    "apologize briefly, say you could not do that right now and ask them to "
    "try again in a little while; do not retry"
)


def state(deps: dict, text: str) -> str:
    deps["_last_tool_state"] = text
    return text


def tz_of(deps: dict) -> str:
    return deps.get("tenant_timezone") or "UTC"


def say_time(iso: str, tz: str) -> str:
    """'Tuesday March 4th at 12:00 PM (noon)'. The hint stops the model
    calling twelve o'clock an afternoon or a midnight time."""
    speech = format_slot_for_speech(iso, tz)
    if speech.endswith("at 12:00 PM"):
        return speech + " (noon)"
    return speech


def local_hour(iso: str, tz: str) -> int:
    value = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
    return datetime.fromisoformat(value).astimezone(ZoneInfo(tz)).hour


# ── slot tokens ────────────────────────────────────────────────────────────

def resolve_slot(deps: dict, token: str) -> dict | None:
    """The registered slot for a token: {slot_start_utc, slot_end_utc, created_at}.

    A missing or unknown token falls back to the one slot the caller was last
    offered, when there is exactly one such offer on the table.
    """
    tokens = deps.get("_slot_tokens") or {}
    if token not in tokens:
        last = deps.get("_last_offered_token")
        token = last if last in tokens else ""
    entry = tokens.get(token)
    if not entry or (time.time() - entry.get("created_at", 0)) >= TOKEN_TTL_S:
        return None
    return entry


def slot_first_offered_at(deps: dict, slot_start_utc: str) -> float:
    return min(
        (
            e.get("created_at", 0)
            for e in (deps.get("_slot_tokens") or {}).values()
            if e.get("slot_start_utc") == slot_start_utc
        ),
        default=0,
    )


# ── appointment tokens ─────────────────────────────────────────────────────

def register_appointment(deps: dict, appointment: dict, *, own: bool = False) -> str:
    """Token for an appointment the caller may act on. One token per
    appointment per call, so a repeated lookup returns the same token.
    ``own`` marks an appointment booked on this call."""
    registry = deps.setdefault("_appt_tokens", {})
    for token, entry in registry.items():
        if entry["appointment"]["id"] == appointment["id"]:
            entry["appointment"].update(appointment)
            entry["own"] = entry.get("own") or own
            return token
    token = "appt_" + secrets.token_hex(4)
    registry[token] = {"appointment": dict(appointment), "created_at": time.time(), "own": own}
    return token


def resolve_appointment(deps: dict, token: str) -> dict | None:
    """The registry entry for a token. With no valid token, the single
    appointment looked up on this call, if there is exactly one."""
    registry = deps.get("_appt_tokens") or {}
    entry = registry.get(token)
    if entry is None and len(registry) == 1:
        entry = next(iter(registry.values()))
    return entry


# ── confirm-then-act gate ──────────────────────────────────────────────────

def unconfirmed(deps: dict, key: str, offered_at: float) -> bool:
    """True, once per key, when the thing being acted on was first put to the
    caller after their last turn, so they cannot have agreed to it yet.

    This is the part of "read it back and wait for a yes" that code can see:
    a lookup and a write in the same model turn. Inactive when the entrypoint
    has recorded no caller turn (unit tests).
    """
    if not CONFIRM_GATE_ENABLED:
        return False
    last_user_turn_at = deps.get("_last_user_turn_at")
    gated = deps.setdefault("_confirm_gate_keys", set())
    if last_user_turn_at is None or offered_at <= last_user_turn_at or key in gated:
        return False
    gated.add(key)
    return True


def mark_write(deps: dict) -> None:
    """A booking change completed: the claim guard stands down until the next
    offer or lookup, and cached availability is stale."""
    deps["_last_write_at"] = time.time()
    deps.pop("_slot_cache", None)
    deps.pop("_last_offered_token", None)
