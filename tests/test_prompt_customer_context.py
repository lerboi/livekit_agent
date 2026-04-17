"""Tests for prompt.py customer_context block injection (Phase 55 Plan 07)."""
from src.prompt import build_system_prompt


def test_no_customer_context_omits_block():
    prompt = build_system_prompt("en", business_name="Acme", customer_context=None)
    assert "CALLER ACCOUNT CONTEXT" not in prompt
    assert "STATE:" not in prompt  # no leakage from any other section


def test_no_match_customer_context_injects_locked_string():
    # ctx present but no contact — tool returns no_xero_contact_for_phone.
    # The prompt block SHOULD still be emitted so the LLM knows "treat as cold".
    prompt = build_system_prompt(
        "en", business_name="Acme", customer_context={"contact": None},
    )
    assert "CALLER ACCOUNT CONTEXT" in prompt
    assert "STATE: no_xero_contact_for_phone" in prompt
    assert "CRITICAL RULE: Treat the STATE above as silent" in prompt


def test_customer_context_injects_block_with_critical_rule():
    ctx = {
        "contact": {"name": "John Smith"},
        "outstanding_balance": 100.0,
        "last_invoices": [
            {"invoice_number": "INV-1", "date": "2026-04-10", "total": 100,
             "amount_due": 100, "status": "AUTHORISED"},
        ],
        "last_payment_date": None,
    }
    prompt = build_system_prompt("en", business_name="Acme", customer_context=ctx)
    assert "CALLER ACCOUNT CONTEXT" in prompt
    assert "STATE: contact=John Smith" in prompt
    assert "CRITICAL RULE: Treat the STATE above as silent" in prompt
    assert "NEVER volunteer the contact name" in prompt


def test_customer_context_block_contains_tool_hint():
    ctx = {"contact": {"name": "X"}, "outstanding_balance": 0,
           "last_invoices": [], "last_payment_date": None}
    prompt = build_system_prompt("en", business_name="Acme", customer_context=ctx)
    assert "check_customer_account" in prompt
