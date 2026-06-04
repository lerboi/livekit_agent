import asyncio
import re

from supabase import Client

SEVERITY = {"emergency": 3, "urgent": 2, "routine": 1}

# Minimum service-name length to attempt a transcript word-boundary match.
# Guards against short/generic names (e.g. "AC", "gas", "tap") matching
# spuriously and over-escalating. Names shorter than this are skipped.
MIN_SERVICE_NAME_LEN = 4


async def apply_owner_rules(
    supabase: Client,
    base_urgency: str,
    tenant_id: str,
    detected_service: str | None = None,
    transcript: str | None = None,
) -> dict:
    try:
        response = await asyncio.to_thread(
            lambda: supabase.table("services")
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

    if not matched_tag and transcript:
        # No explicit detected_service — derive one by matching each active
        # service's real name against the transcript. Word-boundary regex (not
        # naive substring) + a minimum-length guard prevent short/generic names
        # from matching spuriously. First match wins. This is what makes layer3
        # actually fire (classify_call never passes detected_service).
        transcript_lower = transcript.lower()
        for s in services:
            name_lower = (s.get("name") or "").lower().strip()
            if len(name_lower) < MIN_SERVICE_NAME_LEN:
                continue
            if re.search(rf"\b{re.escape(name_lower)}\b", transcript_lower):
                matched_tag = s["urgency_tag"]
                break

    if not matched_tag:
        # Only adopt a service's urgency_tag when the call content actually matched
        # that service. Previously a single-service tenant auto-adopted its one
        # service's tag on EVERY call, over-escalating routine calls. layer1
        # keywords + layer2 LLM remain the emergency floor.
        matched_tag = base_urgency

    base_severity = SEVERITY.get(base_urgency, 1)
    tag_severity = SEVERITY.get(matched_tag, 1)

    if tag_severity > base_severity:
        return {"urgency": matched_tag, "escalated": True}

    return {"urgency": base_urgency, "escalated": False}
