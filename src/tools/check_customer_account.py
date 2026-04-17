"""check_customer_account tool — Phase 55 (XERO-04).

Re-serves the Xero customer context (fetched pre-session by agent.py via
fetch_xero_context_bounded) as a STATE+DIRECTIVE string per the LiveKit
prompt philosophy. Never speakable English. Never re-fetches — the data
was loaded into deps["customer_context"] before session.start (D-08).

Returns the locked no-match string when deps["customer_context"] is None
or lacks a contact (D-11 — caller is indistinguishable from a cold-call
walk-in). Privacy rule per D-10: silent awareness, never volunteer.
"""
from __future__ import annotations

import logging
from typing import Optional

from livekit.agents import function_tool, RunContext

logger = logging.getLogger(__name__)


def format_customer_context_state(ctx: Optional[dict]) -> str:
    """Render cached Xero context as STATE+DIRECTIVE. DRY: shared with prompt.py."""
    if not ctx or not ctx.get("contact"):
        return (
            "STATE: no_xero_contact_for_phone.\n"
            "DIRECTIVE: Treat as new or walk-in customer. Do not claim to have any records on file."
        )

    contact = ctx["contact"]
    outstanding = float(ctx.get("outstanding_balance") or 0)
    invoices = ctx.get("last_invoices") or []
    last_payment = ctx.get("last_payment_date")

    state_parts = [f"contact={contact.get('name', 'unknown')}"]
    if outstanding > 0:
        n_due = sum(
            1 for i in invoices
            if i.get("status") == "AUTHORISED" and (i.get("amount_due") or 0) > 0
        )
        state_parts.append(f"outstanding=${outstanding:.2f} across {n_due} invoices")
    else:
        state_parts.append("outstanding=$0")

    if invoices:
        last = invoices[0]
        state_parts.append(
            f"last_invoice={last.get('invoice_number')} "
            f"${last.get('total')} dated {last.get('date')} "
            f"({(last.get('status') or '').lower()})"
        )

    if last_payment:
        state_parts.append(f"last_payment={last_payment}")

    state = "; ".join(state_parts)
    return (
        f"STATE: {state}.\n"
        "DIRECTIVE: Answer factually only if the caller explicitly asks about their balance, "
        "bill, or recent work. Do not read invoice numbers unless asked. Do not volunteer figures. "
        "If the caller asks 'do you have my info?' confirm presence without specifics "
        "(we have your contact on file). NEVER claim to have verified or confirmed anything you "
        "have not been asked about. NEVER mention outstanding balance unprompted."
    )


def create_check_customer_account_tool(deps: dict):
    """Factory: returns @function_tool closing over deps['customer_context']."""

    @function_tool(
        name="check_customer_account",
        description=(
            "Returns the caller's Xero customer-account context as a STATE+DIRECTIVE block. "
            "Use ONLY when the caller explicitly asks about their balance, bill, recent work, "
            "or confirms they are an existing customer. Never call proactively. "
            "Returns no_xero_contact_for_phone when caller is unknown."
        ),
    )
    async def check_customer_account(context: RunContext) -> str:
        ctx = deps.get("customer_context") if isinstance(deps, dict) else getattr(deps, "customer_context", None)
        result = format_customer_context_state(ctx)
        logger.info(
            "check_customer_account: served (has_contact=%s)",
            bool(ctx and ctx.get("contact")),
        )
        return result

    return check_customer_account
