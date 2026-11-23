"""Appointment rows: the reads and writes behind the appointments agent's tools.

Voco's ``appointments`` table is the lock. ``book_appointment_atomic`` and the
``appointments_no_overlap`` exclusion constraint make a double booking
impossible at the database, whatever the calendar says, so every tool writes
here first and to the external calendar second.

Which appointments a call may look up, move or cancel (``in_scope``):

* production call: appointments booked from the caller's own phone number.
* test or demo call: appointments made by test calls only, never a real
  customer's. A plain test call (no external calendar) also leaves alone any
  appointment that has a calendar event, because it could not update it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ...lib.booking import atomic_book_slot

logger = logging.getLogger("voco-agent")

_APPT_COLUMNS = (
    "id, start_time, end_time, caller_name, caller_phone, notes, status, call_id, "
    "external_event_id, external_event_provider"
)

NO_ADDRESS = "Not applicable"


async def book(deps: dict, *, start_utc: str, end_utc: str, caller_name: str) -> dict:
    """Atomically take the slot. Returns the RPC result ({success, appointment_id, ...})."""
    return await atomic_book_slot(
        deps["supabase"],
        tenant_id=deps["tenant_id"],
        call_id=deps.get("call_uuid") or None,
        start_time=start_utc,
        end_time=end_utc,
        address=NO_ADDRESS,
        caller_name=caller_name or "Caller",
        caller_phone=deps.get("from_number") or "",
        urgency="routine",
        address_validation_verdict="skipped",
    )


async def save_details(deps: dict, appointment_id: str, *, reason: str | None) -> None:
    """Follow-up writes after a booking: the reason, the call link, the outcome."""
    supabase = deps["supabase"]

    async def _notes():
        if not reason:
            return
        await asyncio.to_thread(
            lambda: supabase.table("appointments")
            .update({"notes": reason[:500]})
            .eq("id", appointment_id)
            .execute()
        )

    async def _call_link():
        if not deps.get("call_uuid"):
            return
        await asyncio.to_thread(
            lambda: supabase.table("appointments")
            .update({"call_id": deps["call_uuid"]})
            .eq("id", appointment_id)
            .is_("call_id", "null")
            .execute()
        )

    async def _outcome():
        await asyncio.to_thread(
            lambda: supabase.table("calls")
            .update({"booking_outcome": "booked"})
            .eq("call_id", deps.get("call_id", ""))
            .execute()
        )

    results = await asyncio.gather(_notes(), _call_link(), _outcome(), return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            # Non-critical: post-call reconciles the outcome and the call link.
            logger.warning("[appointments] follow-up write failed: %s", result)


async def set_external_event(deps: dict, appointment_id: str, provider: str, event_id: str) -> None:
    supabase = deps["supabase"]
    await asyncio.to_thread(
        lambda: supabase.table("appointments")
        .update({"external_event_id": event_id, "external_event_provider": provider})
        .eq("id", appointment_id)
        .execute()
    )


async def upcoming(deps: dict) -> list[dict]:
    """Confirmed appointments of this tenant that have not ended, soonest first,
    limited to the ones this call is allowed to touch."""
    supabase = deps["supabase"]
    tenant_id = deps.get("tenant_id")
    if not tenant_id:
        return []
    now_iso = datetime.now(timezone.utc).isoformat()
    result = await asyncio.to_thread(
        lambda: supabase.table("appointments")
        .select(_APPT_COLUMNS)
        .eq("tenant_id", tenant_id)
        .eq("status", "confirmed")
        .gte("end_time", now_iso)
        .order("start_time")
        .limit(100)
        .execute()
    )
    rows = result.data or []
    if not rows:
        return []

    if not deps.get("is_test_call"):
        phone = deps.get("from_number") or ""
        return [r for r in rows if phone and r.get("caller_phone") == phone]

    # Test and demo calls: only appointments that belong to test calls.
    call_ids = sorted({r["call_id"] for r in rows if r.get("call_id")})
    test_call_ids: set = set()
    if call_ids:
        calls = await asyncio.to_thread(
            lambda: supabase.table("calls")
            .select("id")
            .eq("tenant_id", tenant_id)
            .eq("is_test_call", True)
            .in_("id", call_ids)
            .execute()
        )
        test_call_ids = {c["id"] for c in (calls.data or [])}
    writable_provider = deps.get("_calendar_provider")
    scoped = []
    for row in rows:
        if row.get("call_id") not in test_call_ids:
            continue
        event_provider = row.get("external_event_provider") if row.get("external_event_id") else None
        if event_provider and event_provider != writable_provider:
            continue  # this call could not update that calendar event
        scoped.append(row)
    return scoped


def _is_overlap_error(err: Exception) -> bool:
    text = f"{getattr(err, 'code', '')} {getattr(err, 'message', '')} {err}"
    return "23P01" in text or "appointments_no_overlap" in text


async def move(deps: dict, appointment: dict, *, start_utc: str, end_utc: str) -> str:
    """Move an appointment. Returns "moved", "taken" or "gone".

    One UPDATE: the exclusion constraint refuses it if another confirmed
    appointment already holds the new time, so there is no window in which
    the caller has neither slot.
    """
    supabase = deps["supabase"]
    update = {"start_time": start_utc, "end_time": end_utc}
    if deps.get("is_test_call") and deps.get("call_uuid"):
        # Keep the row attached to a test call so Demo Studio cleanup finds it
        # from the call that last changed it.
        update["call_id"] = deps["call_uuid"]
    try:
        result = await asyncio.to_thread(
            lambda: supabase.table("appointments")
            .update(update)
            .eq("id", appointment["id"])
            .eq("tenant_id", deps["tenant_id"])
            .eq("status", "confirmed")
            .execute()
        )
    except Exception as err:  # noqa: BLE001
        if _is_overlap_error(err):
            return "taken"
        raise
    return "moved" if result.data else "gone"


async def cancel(deps: dict, appointment: dict) -> bool:
    supabase = deps["supabase"]
    result = await asyncio.to_thread(
        lambda: supabase.table("appointments")
        .update({"status": "cancelled"})
        .eq("id", appointment["id"])
        .eq("tenant_id", deps["tenant_id"])
        .eq("status", "confirmed")
        .execute()
    )
    return bool(result.data)


async def move_mirror(deps: dict, event_id: str, *, start_utc: str, end_utc: str) -> None:
    """Keep the synced copy of the calendar event in step, so the old time is
    free for the next caller before the provider's webhook catches up."""
    supabase = deps["supabase"]
    await asyncio.to_thread(
        lambda: supabase.table("calendar_events")
        .update({"start_time": start_utc, "end_time": end_utc})
        .eq("tenant_id", deps["tenant_id"])
        .eq("external_id", event_id)
        .execute()
    )


async def delete_mirror(deps: dict, event_id: str) -> None:
    supabase = deps["supabase"]
    await asyncio.to_thread(
        lambda: supabase.table("calendar_events")
        .delete()
        .eq("tenant_id", deps["tenant_id"])
        .eq("external_id", event_id)
        .execute()
    )
