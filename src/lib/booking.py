import asyncio

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
    postal_code: str | None = None,
    street_name: str | None = None,
    # Phase 61 NEW (all kwarg-only, all NULLABLE for backward compat):
    formatted_address: str | None = None,
    place_id: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    address_components: dict | None = None,
    address_validation_verdict: str | None = None,
) -> dict:
    response = await asyncio.to_thread(
        lambda: supabase.rpc(
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
                "p_postal_code": postal_code,
                "p_street_name": street_name,
                # Phase 61 NEW:
                "p_formatted_address": formatted_address,
                "p_place_id": place_id,
                "p_latitude": latitude,
                "p_longitude": longitude,
                "p_address_components": address_components,
                "p_address_validation_verdict": address_validation_verdict,
            },
        ).execute()
    )

    data = response.data
    if isinstance(data, list):
        return data[0] if data else {}
    return data
