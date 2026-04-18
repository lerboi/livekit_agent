"""check_customer_account tool — Phase 56 Plan 06.

Re-serves the MERGED Jobber+Xero customer_context (fetched pre-session by
agent.py via fetch_merged_customer_context_bounded) as a STATE+DIRECTIVE
string per the LiveKit prompt philosophy. Never speakable English. Never
re-fetches — the data was loaded into deps["customer_context"] before
session.start (D-08).

Returns the locked no-match string when deps["customer_context"] is None
(both providers missed). Privacy rule per D-10: silent awareness, never
volunteer. Source annotations per D-08 expose per-field provenance
(Jobber / Xero) without inviting recitation.
"""
from __future__ import annotations

import logging
from typing import Optional

from livekit.agents import function_tool, RunContext

logger = logging.getLogger(__name__)


NO_MATCH_RESPONSE = (
    "STATE: no_customer_match_for_phone.\n"
    "DIRECTIVE: Treat as new or walk-in customer. Do not claim to have any records on file."
)


def format_customer_context_state(ctx: Optional[dict]) -> str:
    """Render merged customer_context as STATE+DIRECTIVE (D-10).

    Shape expected (produced by src.lib.customer_context.merge_customer_context):
      {
        client: {id, name, email}?,
        recentJobs: [{jobNumber, title, status, nextVisitDate?, endAt?}]?,
        lastVisitDate: "YYYY-MM-DD"?,
        outstandingBalance: float?,
        lastInvoices: [...]?,
        lastPaymentDate: "YYYY-MM-DD"?,
        _sources: {<field>: "Jobber"|"Xero"},
      }
    Absent fields are OMITTED from STATE (D-11) — never rendered as null.
    DRY: shared with prompt.py._build_customer_account_section.
    """
    if not ctx:
        return NO_MATCH_RESPONSE

    sources = ctx.get("_sources", {}) or {}
    state_parts = []

    # client
    if ctx.get("client"):
        name = ctx["client"].get("name") or "unknown"
        src = sources.get("client", "?")
        state_parts.append(f"client={name} ({src})")

    # recentJobs
    if ctx.get("recentJobs"):
        src = sources.get("recentJobs", "?")
        job_strs = []
        for j in ctx["recentJobs"]:
            s = f'{j.get("jobNumber")} "{j.get("title")}" status={j.get("status")}'
            if j.get("nextVisitDate"):
                s += f" next_visit={j['nextVisitDate']}"
            if j.get("endAt"):
                s += f" completed={j['endAt']}"
            job_strs.append(s)
        state_parts.append(f"recent_jobs=[{', '.join(job_strs)}] ({src})")

    # lastVisitDate
    if ctx.get("lastVisitDate"):
        src = sources.get("lastVisitDate", "?")
        state_parts.append(f"last_visit={ctx['lastVisitDate']} ({src})")

    # outstandingBalance
    if ctx.get("outstandingBalance") is not None:
        n = len(ctx.get("lastInvoices") or [])
        src = sources.get("outstandingBalance", "?")
        state_parts.append(
            f"outstanding_balance=${ctx['outstandingBalance']} across {n} invoices ({src})"
        )

    # lastPaymentDate
    if ctx.get("lastPaymentDate"):
        src = sources.get("lastPaymentDate", "?")
        state_parts.append(f"last_payment={ctx['lastPaymentDate']} ({src})")

    if not state_parts:
        return NO_MATCH_RESPONSE

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
            "Returns the caller's merged Jobber+Xero customer-account context as a "
            "STATE+DIRECTIVE block. Use ONLY when the caller explicitly asks about their "
            "balance, bill, recent work, or confirms they are an existing customer. Never "
            "call proactively. Returns no_customer_match_for_phone when caller is unknown."
        ),
    )
    async def check_customer_account(context: RunContext) -> str:
        ctx = deps.get("customer_context") if isinstance(deps, dict) else getattr(deps, "customer_context", None)
        result = format_customer_context_state(ctx)
        logger.info(
            "check_customer_account: served (has_ctx=%s sources=%s)",
            bool(ctx),
            list((ctx or {}).get("_sources", {}).values()) if ctx else [],
        )
        return result

    return check_customer_account
