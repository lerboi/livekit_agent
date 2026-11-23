"""A "yes" to a time on offer must make the change.

``confirm_pending_write`` looks at the turn that is about to be generated. If

* the last thing in the conversation is the caller speaking,
* what they said is a plain yes ("Yes, please", "Sure, go ahead"), and
* exactly one time is on offer and has not been acted on,

it returns the tool the turn must call: ``reschedule_appointment`` when the
caller's appointment was looked up since the last change (they are moving it),
otherwise ``book_appointment``. The model still writes the arguments, and the
tool still does its own checks (the token must be one it issued; a time the
caller has not had a turn to accept is refused once). Nothing is decided here
that the caller did not just say yes to.

Why: on a spoken take on 2026-11-24 the agent asked "Shall I book it?", the
caller said "Yes, please", and the model replied "What is the appointment
for?" No booking was made, so the move that followed found nothing to move.

In every other situation it returns None and the turn runs normally.
"""

from __future__ import annotations

import re

_YES_RE = re.compile(
    r"^\W*(?:yes|yeah|yep|yup|sure|ok|okay|please|please do|go ahead|go for it|do it"
    r"|that works|that's fine|that is fine|sounds good|perfect|correct|that's right)\b",
    re.IGNORECASE,
)
# A yes that goes on to change the request is not a plain yes.
_BUT_RE = re.compile(
    r"\b(?:but|actually|instead|wait|no|not|don't|do not|different|another|change|cancel|move)\b",
    re.IGNORECASE,
)
MAX_YES_WORDS = 8


def is_plain_yes(text: str) -> bool:
    text = (text or "").strip()
    if not text or len(text.split()) > MAX_YES_WORDS:
        return False
    return bool(_YES_RE.match(text)) and not _BUT_RE.search(text)


def _last_item(chat_ctx):
    items = getattr(chat_ctx, "items", None) or []
    return items[-1] if items else None


def _user_text(item) -> str | None:
    """The text of ``item`` when it is the caller speaking, else None."""
    if item is None or getattr(item, "type", "message") != "message":
        return None
    if getattr(item, "role", None) != "user":
        return None
    return getattr(item, "text_content", None) or ""


def _looked_up_since_last_write(deps: dict) -> bool:
    """find_appointment has been called since the last booking change."""
    for entry in reversed(deps.get("_tool_call_log") or []):
        name = entry.get("name")
        if name == "find_appointment":
            return True
        if name in ("book_appointment", "reschedule_appointment", "cancel_appointment") and entry.get("success"):
            return False
    return False


def pending_tool(deps: dict) -> str | None:
    """The write the single time on offer is for, or None when none is."""
    token = deps.get("_last_offered_token")
    if not token or token not in (deps.get("_slot_tokens") or {}):
        return None
    has_appointment = any(
        entry["appointment"].get("status") == "confirmed"
        for entry in (deps.get("_appt_tokens") or {}).values()
    )
    if has_appointment and _looked_up_since_last_write(deps):
        return "reschedule_appointment"
    return "book_appointment"


def confirm_pending_write(deps: dict, chat_ctx):
    """(tool_choice, nudge) for a turn that must make the change, else None."""
    text = _user_text(_last_item(chat_ctx))
    if text is None or not is_plain_yes(text):
        return None
    tool = pending_tool(deps)
    if tool is None:
        return None
    return {"type": "function", "function": {"name": tool}}, None
