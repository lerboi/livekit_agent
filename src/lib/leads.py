import logging

from supabase import Client

logger = logging.getLogger(__name__)


async def create_or_merge_lead(
    supabase: Client,
    *,
    tenant_id: str,
    call_id: str,
    from_number: str,
    caller_name: str | None = None,
    job_type: str | None = None,
    service_address: str | None = None,
    triage_result: dict | None = None,
    appointment_id: str | None = None,
    call_duration: int | float = 0,
) -> dict | None:
    if call_duration < 15:
        return None

    # Check for existing lead from same number
    response = (
        supabase.table("leads")
        .select("id, status")
        .eq("tenant_id", tenant_id)
        .eq("from_number", from_number)
        .in_("status", ["new", "booked"])
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    existing_leads = response.data

    if existing_leads and len(existing_leads) > 0:
        existing_lead = existing_leads[0]
        supabase.table("lead_calls").insert(
            {"lead_id": existing_lead["id"], "call_id": call_id}
        ).execute()
        return existing_lead

    # Create new lead
    new_lead_status = "booked" if appointment_id else "new"
    urgency = (triage_result or {}).get("urgency", "routine")

    insert_response = (
        supabase.table("leads")
        .insert(
            [
                {
                    "tenant_id": tenant_id,
                    "from_number": from_number,
                    "caller_name": caller_name or None,
                    "job_type": job_type or None,
                    "service_address": service_address or None,
                    "urgency": urgency,
                    "status": new_lead_status,
                    "primary_call_id": call_id,
                    "appointment_id": appointment_id or None,
                }
            ]
        )
        .select("id, status, from_number, urgency, caller_name, job_type")
        .execute()
    )

    if not insert_response.data:
        logger.error("create_or_merge_lead: insert returned no data")
        raise Exception("create_or_merge_lead: insert returned no data")

    new_lead = insert_response.data[0]

    # Link call to lead
    supabase.table("lead_calls").insert(
        {"lead_id": new_lead["id"], "call_id": call_id}
    ).execute()

    # Log activity
    supabase.table("activity_log").insert(
        {
            "tenant_id": tenant_id,
            "event_type": "lead_created",
            "lead_id": new_lead["id"],
            "metadata": {
                "caller_name": caller_name or None,
                "job_type": job_type or None,
                "urgency": urgency,
            },
        }
    ).execute()

    return new_lead
