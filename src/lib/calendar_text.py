"""Calendar event wording shared by the Google and Outlook pushes."""

from __future__ import annotations

SCHEDULING_BODY = "Booked by the Voco voice agent"


def scheduling_event_text(appointment: dict) -> tuple[str, str]:
    """Title and body for an appointment booked by the scheduling profile.

    Scheduling appointments have no service address and no urgency, so the
    event shows who it is with and what it is for (``appointments.notes``
    holds the reason the caller gave): ``Alex Tan: Product demo``.
    """
    caller = (appointment.get("caller_name") or "").strip()
    if not caller or caller.lower() == "caller":
        caller = ""
    reason = (appointment.get("notes") or "").strip()
    reason = reason[:1].upper() + reason[1:] if reason else ""
    if caller and reason:
        title = f"{caller}: {reason}"
    else:
        title = caller or reason or "Appointment"
    return title[:200], SCHEDULING_BODY
