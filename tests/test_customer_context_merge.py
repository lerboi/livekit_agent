"""Tests for merge_customer_context (Phase 56 Plan 06).

Field-level merge per CONTEXT D-07:
  Jobber wins: client, recentJobs, lastVisitDate
  Xero wins:   outstandingBalance, lastPaymentDate, lastInvoices

NOTE: Xero's fetch_xero_customer_by_phone returns SNAKE_CASE keys
(`contact`, `outstanding_balance`, `last_invoices`, `last_payment_date`).
The merge helper normalizes both providers into a single camelCase
output shape consumed by prompt.py + check_customer_account.
"""
import pytest
from src.lib.customer_context import merge_customer_context


def test_M1_both_none_returns_none():
    assert merge_customer_context(jobber=None, xero=None) is None


def test_M2_only_jobber():
    jobber = {
        "client": {"id": "j1", "name": "John", "email": "j@e.com"},
        "recentJobs": [
            {"jobNumber": "J-1", "title": "AC install", "status": "upcoming",
             "startAt": None, "endAt": None, "nextVisitDate": "2026-04-20"}
        ],
        "outstandingInvoices": [
            {"invoiceNumber": "INV-1", "issuedAt": "2026-04-01", "amount": 100,
             "amountOutstanding": 100, "status": "AWAITING_PAYMENT"}
        ],
        "outstandingBalance": 100.0,
        "lastVisitDate": "2026-04-15",
    }
    r = merge_customer_context(jobber=jobber, xero=None)
    assert r is not None
    assert r["client"]["id"] == "j1"
    assert r["recentJobs"][0]["jobNumber"] == "J-1"
    assert r["lastVisitDate"] == "2026-04-15"
    assert r["outstandingBalance"] == 100.0  # fallback to Jobber
    assert r["_sources"]["client"] == "Jobber"
    assert r["_sources"]["outstandingBalance"] == "Jobber"
    # Xero-only fields omitted
    assert "lastPaymentDate" not in r
    # lastInvoices falls back to Jobber.outstandingInvoices
    assert r["lastInvoices"][0]["invoiceNumber"] == "INV-1"
    assert r["_sources"]["lastInvoices"] == "Jobber"


def test_M3_only_xero():
    # Xero's real snake_case shape from fetch_xero_customer_by_phone
    xero = {
        "contact": {
            "contact_id": "x1", "name": "John Smith",
            "first_name": "John", "last_name": "Smith",
            "phones": ["+15551234567"],
        },
        "outstanding_balance": 500.0,
        "last_invoices": [
            {"invoice_number": "INV-X", "date": "2026-04-01", "total": 500,
             "amount_due": 500, "status": "AUTHORISED"}
        ],
        "last_payment_date": "2026-03-15",
    }
    r = merge_customer_context(jobber=None, xero=xero)
    assert r is not None
    # client renamed from contact
    assert r["client"]["name"] == "John Smith"
    assert r["outstandingBalance"] == 500.0
    assert r["lastPaymentDate"] == "2026-03-15"
    assert r["lastInvoices"][0]["invoice_number"] == "INV-X"
    assert r["_sources"]["client"] == "Xero"
    assert r["_sources"]["outstandingBalance"] == "Xero"
    # Jobber-only fields omitted
    assert "recentJobs" not in r
    assert "lastVisitDate" not in r


def test_M4_both_present_jobber_wins_operations_xero_wins_payments():
    jobber = {
        "client": {"id": "j1", "name": "John Jobber", "email": "jj@e.com"},
        "recentJobs": [
            {"jobNumber": "J-1", "title": "AC", "status": "upcoming",
             "startAt": None, "endAt": None, "nextVisitDate": "2026-04-20"}
        ],
        "outstandingInvoices": [],
        "outstandingBalance": 100.0,
        "lastVisitDate": "2026-04-15",
    }
    xero = {
        "contact": {"contact_id": "x1", "name": "John Xero"},
        "outstanding_balance": 0.0,  # Xero reconciled; Jobber is stale
        "last_invoices": [
            {"invoice_number": "INV-X", "date": "2026-04-01", "total": 500,
             "amount_due": 0, "status": "PAID"}
        ],
        "last_payment_date": "2026-04-05",
    }
    r = merge_customer_context(jobber=jobber, xero=xero)
    assert r is not None
    # Jobber wins operations
    assert r["client"]["name"] == "John Jobber"
    assert r["_sources"]["client"] == "Jobber"
    assert r["recentJobs"][0]["jobNumber"] == "J-1"
    assert r["lastVisitDate"] == "2026-04-15"
    # Xero wins payments
    assert r["outstandingBalance"] == 0.0
    assert r["_sources"]["outstandingBalance"] == "Xero"
    assert r["lastPaymentDate"] == "2026-04-05"
    assert r["lastInvoices"][0]["invoice_number"] == "INV-X"
    assert r["_sources"]["lastInvoices"] == "Xero"


def test_M5_fallback_outstanding_to_jobber_when_xero_missing_field():
    jobber = {
        "client": {"id": "j1", "name": "John"},
        "recentJobs": [], "outstandingInvoices": [],
        "outstandingBalance": 200.0, "lastVisitDate": None,
    }
    xero = {
        "contact": {"contact_id": "x1", "name": "John"},
        "outstanding_balance": None,  # not present
        "last_invoices": [], "last_payment_date": "2026-04-01",
    }
    r = merge_customer_context(jobber=jobber, xero=xero)
    assert r is not None
    assert r["outstandingBalance"] == 200.0
    assert r["_sources"]["outstandingBalance"] == "Jobber"
    assert r["lastPaymentDate"] == "2026-04-01"
    assert r["_sources"]["lastPaymentDate"] == "Xero"
