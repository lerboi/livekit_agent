from supabase import Client

from .layer1_keywords import run_keyword_classifier
from .layer2_llm import run_llm_scorer
from .layer3_rules import apply_owner_rules

VALID_URGENCIES = {"emergency", "routine", "high_ticket"}


def _sanitize_urgency(urgency: str) -> str:
    """Ensure urgency is a valid DB enum value."""
    if urgency in VALID_URGENCIES:
        return urgency
    return "routine"


async def classify_call(
    supabase: Client,
    *,
    transcript: str | None,
    tenant_id: str,
    detected_service: str | None = None,
) -> dict:
    if not transcript or len(transcript) < 10:
        return {"urgency": "routine", "confidence": "low", "layer": "layer1"}

    layer1_result = run_keyword_classifier(transcript)

    if layer1_result["confident"]:
        layer3_result = await apply_owner_rules(
            supabase, layer1_result["result"], tenant_id, detected_service
        )
        final_layer = "layer3" if layer3_result["escalated"] else "layer1"
        return {
            "urgency": layer3_result["urgency"],
            "confidence": "high",
            "layer": final_layer,
        }

    layer2_result = await run_llm_scorer(transcript)
    layer2_urgency = _sanitize_urgency(layer2_result.get("urgency", "routine"))
    layer3_result = await apply_owner_rules(
        supabase, layer2_urgency, tenant_id, detected_service
    )
    final_layer = "layer3" if layer3_result["escalated"] else "layer2"

    return {
        "urgency": _sanitize_urgency(layer3_result["urgency"]),
        "confidence": layer2_result.get("confidence", "low"),
        "layer": final_layer,
        "reason": layer2_result.get("reason"),
    }
