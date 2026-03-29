from supabase import Client


async def atomic_book_slot(
    supabase: Client,
    *,
    tenant_id: str,
    call_id: str,
    start_time: str,
    end_time: str,
    address: str,
    caller_name: str,
    caller_phone: str,
    urgency: str,
    zone_id: str | None = None,
) -> dict:
    response = supabase.rpc(
        "book_appointment_atomic",
        {
            "p_tenant_id": tenant_id,
            "p_call_id": call_id,
            "p_start_time": start_time,
            "p_end_time": end_time,
            "p_service_address": address,
            "p_caller_name": caller_name,
            "p_caller_phone": caller_phone,
            "p_urgency": urgency,
            "p_zone_id": zone_id,
        },
    ).execute()

    if hasattr(response, "error") and response.error:
        raise Exception(f"book_appointment_atomic failed: {response.error}")

    data = response.data
    if isinstance(data, list):
        return data[0] if data else {}
    return data
