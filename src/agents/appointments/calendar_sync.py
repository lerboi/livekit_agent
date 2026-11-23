"""Write a booking change to the external calendar and report how it went.

The tool that calls ``sync`` is holding the caller's turn, so the write gets a
short budget (``CALENDAR_WAIT_S``). Within it the result is known and the tool
can say so; past it the write carries on in the background and the tool
returns "pending". A failed write is retried once. Every outcome is published
on the live event stream (web test rooms only) so the Demo Studio shows the
calendar event appear, move or disappear.

Nothing here raises: the appointment is already safe in Voco's own table.
"""

from __future__ import annotations

import asyncio
import logging
import os

from ...calendars import CalendarError
from ...lib.background import create_background_task
from ...lib.calendar_text import scheduling_event_text
from ...lib.demo import publish_event
from . import store

logger = logging.getLogger("voco-agent")

CALENDAR_WAIT_S = float(os.environ.get("VOCO_CALENDAR_WAIT_S", "2.5"))
CALENDAR_OPEN_WAIT_S = 4.0
RETRY_DELAY_S = 1.0

SAVED, PENDING, FAILED, NONE = "saved", "pending", "failed", "none"


async def get_calendar(deps: dict):
    """The call's calendar connection, or None when this call may not write to
    one or none is connected. The entrypoint opens it during the greeting."""
    pending = deps.get("_calendar_task")
    if pending is None:
        return deps.get("_calendar")
    try:
        deps["_calendar"] = await asyncio.wait_for(asyncio.shield(pending), CALENDAR_OPEN_WAIT_S)
    except Exception as err:  # noqa: BLE001
        logger.warning("[calendar] connection not ready: %s", err)
        return None
    return deps["_calendar"]


async def _attempt(operation):
    """Run a blocking calendar call, once more after a short pause if it fails."""
    try:
        return await asyncio.to_thread(operation)
    except Exception as first:  # noqa: BLE001
        logger.warning("[calendar] write failed, retrying once: %s", first)
        await asyncio.sleep(RETRY_DELAY_S)
        return await asyncio.to_thread(operation)


async def _quietly(write) -> None:
    """Bookkeeping after a calendar write that already succeeded. A failure
    here is logged, not reported as a failed calendar write."""
    try:
        await write
    except Exception as err:  # noqa: BLE001
        logger.error("[calendar] bookkeeping write failed: %s", err)


async def _bounded(deps: dict, work, *, appointment_id: str, provider: str, action: str) -> str:
    async def _run() -> str:
        try:
            await work()
            return SAVED
        except Exception as err:  # noqa: BLE001
            level = logging.WARNING if isinstance(err, CalendarError) else logging.ERROR
            logger.log(level, "[calendar] %s failed for appointment=%s: %s", action, appointment_id, err)
            publish_event(
                deps, "calendar_failed",
                appointment_id=appointment_id, provider=provider, action=action,
            )
            return FAILED

    task = create_background_task(_run(), name=f"calendar-{action}")
    done, _ = await asyncio.wait({task}, timeout=CALENDAR_WAIT_S)
    return task.result() if done else PENDING


async def create(deps: dict, appointment: dict) -> str:
    """Put a new appointment on the calendar."""
    calendar = await get_calendar(deps)
    if calendar is None:
        return NONE
    title, body = scheduling_event_text(appointment)
    tz = deps.get("tenant_timezone") or "UTC"

    async def _work():
        event = await _attempt(lambda: calendar.create_event(
            title=title, body=body,
            start_utc=appointment["start_time"], end_utc=appointment["end_time"], tz=tz,
        ))
        appointment["external_event_id"] = event["event_id"]
        appointment["external_event_provider"] = calendar.provider
        publish_event(
            deps, "calendar_saved",
            appointment_id=appointment["id"], provider=calendar.provider,
            event_id=event["event_id"], link=event.get("html_link"),
        )
        await _quietly(store.set_external_event(
            deps, appointment["id"], calendar.provider, event["event_id"],
        ))

    return await _bounded(
        deps, _work, appointment_id=appointment["id"], provider=calendar.provider, action="create",
    )


async def move(deps: dict, appointment: dict) -> str:
    """Move the appointment's calendar event to its new time. An appointment
    that never got an event (an earlier write failed) gets one now."""
    calendar = await get_calendar(deps)
    if calendar is None:
        return NONE
    event_id = appointment.get("external_event_id")
    if not event_id:
        return await create(deps, appointment)
    if appointment.get("external_event_provider") != calendar.provider:
        return NONE
    tz = deps.get("tenant_timezone") or "UTC"

    async def _work():
        event = await _attempt(lambda: calendar.move_event(
            event_id, start_utc=appointment["start_time"], end_utc=appointment["end_time"], tz=tz,
        ))
        publish_event(
            deps, "calendar_updated",
            appointment_id=appointment["id"], provider=calendar.provider,
            event_id=event_id, link=event.get("html_link"),
        )
        await _quietly(store.move_mirror(
            deps, event_id,
            start_utc=appointment["start_time"], end_utc=appointment["end_time"],
        ))

    return await _bounded(
        deps, _work, appointment_id=appointment["id"], provider=calendar.provider, action="move",
    )


async def remove(deps: dict, appointment: dict) -> str:
    """Take a cancelled appointment's event off the calendar."""
    calendar = await get_calendar(deps)
    event_id = appointment.get("external_event_id")
    if calendar is None or not event_id:
        return NONE
    if appointment.get("external_event_provider") != calendar.provider:
        return NONE

    async def _work():
        await _attempt(lambda: calendar.delete_event(event_id))
        publish_event(
            deps, "calendar_removed",
            appointment_id=appointment["id"], provider=calendar.provider, event_id=event_id,
        )
        await _quietly(store.delete_mirror(deps, event_id))

    return await _bounded(
        deps, _work, appointment_id=appointment["id"], provider=calendar.provider, action="remove",
    )
