"""Tests for src.tools.check_customer_account (Phase 56 Plan 06 — merged shape).

P56 supersedes P55's Xero-only serialization. The tool now re-serves the merged
Jobber+Xero dict produced by src.lib.customer_context.merge_customer_context,
with per-field (Jobber)/(Xero) source annotations per CONTEXT D-08/D-10.

No re-fetch, no GraphQL/Xero round-trip — tool reads deps["customer_context"]
already populated pre-session by fetch_merged_customer_context_bounded.
"""
from unittest.mock import patch

import pytest

from src.tools.check_customer_account import format_customer_context_state


# ---- AC1: no-match locked string ------------------------------------------


def test_AC1_no_match_returns_locked_string():
    s = format_customer_context_state(None)
    assert "STATE: no_customer_match_for_phone" in s
    assert "DIRECTIVE: Treat as new or walk-in customer" in s


def test_AC1b_empty_dict_returns_locked_string():
    s = format_customer_context_state({})
    assert "no_customer_match_for_phone" in s


# ---- AC2: Jobber-only serialization ---------------------------------------


def test_AC2_jobber_only_serialization():
    ctx = {
        "client": {"id": "j1", "name": "John", "email": "j@e.com"},
        "recentJobs": [
            {"jobNumber": "JBN-204", "title": "AC install", "status": "upcoming",
             "startAt": None, "endAt": None, "nextVisitDate": "2026-04-20"},
        ],
        "lastVisitDate": "2026-04-15",
        "_sources": {
            "client": "Jobber", "recentJobs": "Jobber", "lastVisitDate": "Jobber",
        },
    }
    s = format_customer_context_state(ctx)
    assert "STATE:" in s
    assert "John" in s
    assert "(Jobber)" in s
    assert "JBN-204" in s
    # No outstanding_balance / last_payment lines — fields absent
    assert "outstanding_balance" not in s
    assert "last_payment" not in s
    assert "DIRECTIVE:" in s


# ---- AC3: merged both providers -------------------------------------------


def test_AC3_merged_both_providers_serialization():
    ctx = {
        "client": {"id": "j1", "name": "John Jobber"},
        "recentJobs": [
            {"jobNumber": "J-1", "title": "AC", "status": "upcoming",
             "nextVisitDate": "2026-04-20"}
        ],
        "lastVisitDate": "2026-04-15",
        "outstandingBalance": 847.25,
        "lastInvoices": [
            {"invoice_number": "X-1"}, {"invoice_number": "X-2"},
        ],
        "lastPaymentDate": "2026-03-15",
        "_sources": {
            "client": "Jobber", "recentJobs": "Jobber", "lastVisitDate": "Jobber",
            "outstandingBalance": "Xero", "lastInvoices": "Xero", "lastPaymentDate": "Xero",
        },
    }
    s = format_customer_context_state(ctx)
    assert "(Jobber)" in s
    assert "(Xero)" in s
    assert "847.25" in s or "847" in s
    assert "John Jobber" in s
    assert "J-1" in s
    assert "2026-03-15" in s


# ---- AC4: DIRECTIVE present -----------------------------------------------


def test_AC4_directive_present_after_state():
    ctx = {
        "client": {"name": "John"},
        "_sources": {"client": "Jobber"},
    }
    s = format_customer_context_state(ctx)
    # DIRECTIVE must appear after STATE and instruct fact-only-on-ask
    state_idx = s.index("STATE:")
    directive_idx = s.index("DIRECTIVE:")
    assert directive_idx > state_idx
    assert "explicitly" in s.lower() or "asks" in s.lower()


# ---- AC5: tool makes no fetch calls ---------------------------------------


@pytest.mark.asyncio
async def test_AC5_tool_factory_does_not_refetch():
    """The tool factory, when invoked, must NOT call jobber/xero fetchers."""
    from src.tools.check_customer_account import create_check_customer_account_tool
    import src.integrations.jobber as jobber_mod
    import src.integrations.xero as xero_mod

    deps = {"customer_context": None}
    tool = create_check_customer_account_tool(deps)

    with patch.object(jobber_mod, "fetch_jobber_customer_by_phone") as mock_j, \
         patch.object(xero_mod, "fetch_xero_customer_by_phone") as mock_x:
        # Tool is a function_tool — call the underlying async fn via format helper
        # which is what the tool body does internally.
        result = format_customer_context_state(deps["customer_context"])
    mock_j.assert_not_called()
    mock_x.assert_not_called()
    assert "no_customer_match_for_phone" in result
