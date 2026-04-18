"""Tests for prompt.py customer_context block injection (Phase 56 Plan 06).

Supersedes the P55 Xero-only tests — the prompt block now renders the merged
Jobber+Xero dict per CONTEXT D-09 with (Jobber)/(Xero) source annotations.
"""
from src.prompt import build_system_prompt


def test_no_customer_context_omits_block():
    prompt = build_system_prompt("en", business_name="Acme", customer_context=None)
    assert "CRITICAL RULE — CUSTOMER CONTEXT" not in prompt
    assert "STATE:" not in prompt  # no leakage from any other section


def test_jobber_only_context_renders_with_source():
    ctx = {
        "client": {"id": "j1", "name": "John", "email": "j@e.com"},
        "recentJobs": [
            {"jobNumber": "JBN-204", "title": "AC install", "status": "upcoming",
             "nextVisitDate": "2026-04-20"},
        ],
        "lastVisitDate": "2026-04-15",
        "_sources": {
            "client": "Jobber", "recentJobs": "Jobber", "lastVisitDate": "Jobber",
        },
    }
    prompt = build_system_prompt("en", business_name="Acme", customer_context=ctx)
    assert "CRITICAL RULE — CUSTOMER CONTEXT" in prompt
    assert "STATE:" in prompt
    assert "John" in prompt
    assert "(Jobber)" in prompt
    assert "JBN-204" in prompt
    # Xero-only fields omitted when Xero missed
    assert "outstanding_balance" not in prompt
    assert "last_payment=" not in prompt
    assert "DIRECTIVE:" in prompt


def test_merged_context_renders_mixed_sources():
    ctx = {
        "client": {"name": "John"},
        "recentJobs": [
            {"jobNumber": "J-1", "title": "AC", "status": "upcoming",
             "nextVisitDate": "2026-04-20"},
        ],
        "lastVisitDate": "2026-04-15",
        "outstandingBalance": 847.25,
        "lastInvoices": [{"invoice_number": "X-1"}, {"invoice_number": "X-2"}],
        "lastPaymentDate": "2026-03-15",
        "_sources": {
            "client": "Jobber", "recentJobs": "Jobber", "lastVisitDate": "Jobber",
            "outstandingBalance": "Xero", "lastInvoices": "Xero", "lastPaymentDate": "Xero",
        },
    }
    prompt = build_system_prompt("en", business_name="Acme", customer_context=ctx)
    assert "(Jobber)" in prompt
    assert "(Xero)" in prompt
    assert "847.25" in prompt or "847" in prompt
    assert "2026-03-15" in prompt


def test_critical_rule_phrasing_locked():
    ctx = {"client": {"name": "X"}, "_sources": {"client": "Jobber"}}
    prompt = build_system_prompt("en", business_name="Acme", customer_context=ctx)
    # Anti-hallucination phrasing must be retained verbatim per D-09
    assert "Never volunteer" in prompt
    assert 'do you have my info' in prompt.lower()
