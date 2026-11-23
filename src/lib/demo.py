"""Demo Studio support: the server-set ``demo`` room-metadata block and the
live event stream the admin pages render.

A demo call is still a sandboxed test call (no billing, no SMS, no CRM rows,
no owner notifications). The ``demo`` block relaxes exactly two sandbox rules:

* ``destination`` = "google" | "outlook": the booking is pushed to that
  external calendar even though it is a test call.
* ``keep_booking`` = true: post-call does not auto-cancel the appointment
  (the Demo Studio's cleanup button removes it instead).

It can also carry ``business_name``: the business the agent answers as on that
call, for a recording that should not say the demo tenant's name. It changes
the greeting and the name in the prompt and nothing else: the tenant, its
hours, its calendar and every tool are the real ones.

Room metadata is written by the admin-gated Next.js session route; a browser
join token cannot change it. The block is ignored unless ``test_call`` is true,
so nothing here can alter a production call.
"""

from __future__ import annotations

import json
import logging
import re
import time

from .background import create_background_task

logger = logging.getLogger("voco-agent")

EVENTS_TOPIC = "voco.events"
EXTERNAL_DESTINATIONS = ("google", "outlook")
_DESTINATIONS = EXTERNAL_DESTINATIONS + ("voco",)


def parse_demo(room_meta: dict | None, is_test_call: bool) -> dict | None:
    """Return the normalized demo block, or None when it must be ignored."""
    if not is_test_call or not isinstance(room_meta, dict):
        return None
    raw = room_meta.get("demo")
    if not isinstance(raw, dict):
        return None
    destination = str(raw.get("destination") or "voco").strip().lower()
    if destination not in _DESTINATIONS:
        destination = "voco"
    demo = {
        "task": str(raw.get("task") or "scheduling").strip().lower()[:40],
        "destination": destination,
        "keep_booking": raw.get("keep_booking") is True,
    }
    business_name = clean_business_name(raw.get("business_name"))
    if business_name:
        demo["business_name"] = business_name
    return demo


_BUSINESS_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 &'.\-]{1,39}")


def clean_business_name(value) -> str | None:
    """The business a demo call answers as, or None.

    A recorded demo names a believable business ("Brightside Dental") instead
    of the demo tenant. The name goes into the greeting and the prompt, so it
    is limited to a short run of plain name characters; anything else is
    dropped and the call answers as the tenant.
    """
    if not isinstance(value, str) or any(ord(ch) < 32 for ch in value):
        return None
    name = " ".join(value.split())
    return name if _BUSINESS_NAME_RE.fullmatch(name) else None


def demo_external_destination(deps: dict) -> str | None:
    """The external calendar a demo call asked for, else None.

    None for production calls, plain test calls, and demo calls whose
    destination is the Voco calendar only.
    """
    if not deps.get("is_test_call"):
        return None
    destination = (deps.get("demo") or {}).get("destination")
    return destination if destination in EXTERNAL_DESTINATIONS else None


def keeps_booking(demo: dict | None, is_test_call: bool) -> bool:
    return bool(is_test_call and isinstance(demo, dict) and demo.get("keep_booking") is True)


def publish_event(deps: dict, event_type: str, **payload) -> None:
    """Send one JSON event to the browser on the ``voco.events`` text stream.

    Enabled only for web test rooms (``deps["_events_room"]`` is set by the
    entrypoint). Fire-and-forget and never raises: the event stream is a
    display aid and must not affect the call.
    """
    room = deps.get("_events_room")
    if room is None:
        return
    try:
        body = json.dumps(
            {"type": event_type, "ts": int(time.time() * 1000), **payload},
            default=str,
        )

        async def _send():
            try:
                await room.local_participant.send_text(body, topic=EVENTS_TOPIC)
            except Exception as err:  # noqa: BLE001
                logger.debug("[demo] event %s not sent: %s", event_type, err)

        create_background_task(_send())
    except Exception as err:  # noqa: BLE001
        logger.debug("[demo] event %s not queued: %s", event_type, err)
