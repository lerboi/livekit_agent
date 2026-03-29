from supabase import Client

SEVERITY = {"emergency": 3, "high_ticket": 2, "routine": 1}


async def apply_owner_rules(
    supabase: Client,
    base_urgency: str,
    tenant_id: str,
    detected_service: str | None = None,
) -> dict:
    try:
        response = (
            supabase.table("services")
            .select("name, urgency_tag")
            .eq("tenant_id", tenant_id)
            .eq("is_active", True)
            .execute()
        )
        services = response.data
    except Exception:
        return {"urgency": base_urgency, "escalated": False}

    if not services:
        return {"urgency": base_urgency, "escalated": False}

    matched_tag = None

    if detected_service:
        normalized_detected = detected_service.lower()
        for s in services:
            sname = s["name"].lower()
            if normalized_detected in sname or sname in normalized_detected:
                matched_tag = s["urgency_tag"]
                break

    if not matched_tag:
        if len(services) == 1:
            matched_tag = services[0]["urgency_tag"]
        else:
            matched_tag = base_urgency

    base_severity = SEVERITY.get(base_urgency, 1)
    tag_severity = SEVERITY.get(matched_tag, 1)

    if tag_severity > base_severity:
        return {"urgency": matched_tag, "escalated": True}

    return {"urgency": base_urgency, "escalated": False}
