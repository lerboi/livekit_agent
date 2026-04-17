"""Tests for src.tools.check_customer_account (Phase 55 Plan 07)."""
from src.tools.check_customer_account import format_customer_context_state


def test_no_match_returns_locked_string():
    s = format_customer_context_state(None)
    assert "STATE: no_xero_contact_for_phone" in s
    assert "DIRECTIVE: Treat as new or walk-in customer" in s


def test_no_contact_in_ctx_returns_locked_string():
    s = format_customer_context_state({"contact": None})
    assert "no_xero_contact_for_phone" in s


def test_full_context_includes_balance_and_last_invoice():
    ctx = {
        "contact": {"name": "John Smith"},
        "outstanding_balance": 847.25,
        "last_invoices": [
            {"invoice_number": "INV-1042", "date": "2026-04-10", "total": 500, "amount_due": 500, "status": "AUTHORISED"},
            {"invoice_number": "INV-1041", "date": "2026-04-01", "total": 347.25, "amount_due": 347.25, "status": "AUTHORISED"},
        ],
        "last_payment_date": "2026-03-15",
    }
    s = format_customer_context_state(ctx)
    assert "contact=John Smith" in s
    assert "outstanding=$847.25" in s
    assert "across 2 invoices" in s
    assert "INV-1042" in s
    assert "last_payment=2026-03-15" in s
    assert "DIRECTIVE: Answer factually only if the caller explicitly asks" in s
    assert "Do not volunteer figures" in s


def test_zero_balance_omits_invoice_count():
    ctx = {
        "contact": {"name": "Jane Doe"},
        "outstanding_balance": 0,
        "last_invoices": [],
        "last_payment_date": None,
    }
    s = format_customer_context_state(ctx)
    assert "outstanding=$0" in s
    assert "across" not in s
