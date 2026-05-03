"""Phase 61 Plan 04 — book_appointment + capture_lead tool descriptions encode
the address-validation precondition (D-E1).

Gemini 3.1 Flash Live reads tool descriptions during function-call decision
making — the description is a prompt surface, not just metadata. Phase 61
extends both descriptions to encode that the address fields will be validated
against an external service before the underlying RPC executes, and that the
tool return tells the agent which verdict path to follow when speaking back.

Outcome-framed (per memory feedback_livekit_prompt_philosophy.md): describes
WHAT happens (validation runs, verdict drives readback) rather than usage
hints. The prohibited-phrase rule lives in the prompt's CRITICAL RULE block
(D-E3, see test_prompt_address_validation_rule.py); the tool description's
job is to tell Gemini "consult the verdict in my return value before
speaking" without prescribing exact wording.

Invariants asserted here:
1. book_appointment description mentions validation (validated|validation) AND
   address — the precondition is part of the function-declaration surface.
2. book_appointment description mentions that the tool return tells the agent
   which case applied (confirmed/corrected/unverified) — outcome-framed.
3. capture_lead description: same precondition language (D-E1 symmetry).
4. capture_lead description: same outcome-framed return-branch language.
"""
from __future__ import annotations


# ----- book_appointment -----


def test_book_appointment_description_mentions_validation():
    from src.tools.book_appointment import _BOOK_APPOINTMENT_SCHEMA

    desc = _BOOK_APPOINTMENT_SCHEMA["description"].lower()
    assert ("validated" in desc) or ("validation" in desc), (
        "book_appointment description must mention validation/validated"
    )
    assert "address" in desc, "book_appointment description must mention address"


def test_book_appointment_description_mentions_tool_return_branches():
    from src.tools.book_appointment import _BOOK_APPOINTMENT_SCHEMA

    desc = _BOOK_APPOINTMENT_SCHEMA["description"].lower()
    # The tool return tells the agent whether the address was confirmed,
    # corrected, or could not be verified. Outcome-framed wording — accept
    # either "confirmed/corrected/verified" trio or a "tool return will
    # indicate" + "verdict/verified" pairing.
    has_branches = (
        ("confirmed" in desc and "corrected" in desc)
        or ("verdict" in desc)
        or ("verified" in desc and ("confirmed" in desc or "corrected" in desc))
    )
    assert has_branches, (
        "book_appointment description must mention that the tool return "
        "indicates the validation outcome (confirmed / corrected / "
        "could-not-verify). Got: " + repr(desc)
    )


# ----- capture_lead -----


def test_capture_lead_description_mentions_validation():
    from src.tools.capture_lead import create_capture_lead_tool

    tool = create_capture_lead_tool({"tenant_id": "x", "supabase": None})
    # FunctionTool stores description on .info per livekit-agents 1.5.6.
    desc = (
        getattr(getattr(tool, "info", None), "description", None)
        or getattr(tool, "description", None)
        or ""
    ).lower()
    assert ("validated" in desc) or ("validation" in desc), (
        "capture_lead description must mention validation/validated"
    )
    assert "address" in desc, "capture_lead description must mention address"


def test_capture_lead_description_mentions_tool_return_branches():
    from src.tools.capture_lead import create_capture_lead_tool

    tool = create_capture_lead_tool({"tenant_id": "x", "supabase": None})
    desc = (
        getattr(getattr(tool, "info", None), "description", None)
        or getattr(tool, "description", None)
        or ""
    ).lower()
    has_branches = (
        ("confirmed" in desc and "corrected" in desc)
        or ("verdict" in desc)
        or ("verified" in desc and ("confirmed" in desc or "corrected" in desc))
    )
    assert has_branches, (
        "capture_lead description must mention that the tool return "
        "indicates the validation outcome (confirmed / corrected / "
        "could-not-verify). Got: " + repr(desc)
    )
