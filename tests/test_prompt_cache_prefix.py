"""2026-09-01 prompt caching — cache-aware section layout in build_system_prompt.

OpenAI prompt caching serves the longest byte-identical prefix of a request
(system message + tool schemas + messages). The per-caller blocks (caller
history, CRM/accounting customer context) used to sit at index 12 of 18 in the
section list, so a repeat caller's prompt diverged from the tenant-stable
prefix ~18k chars in and only about half of it could be served from cache
across calls. They now trail every tenant-stable section, directly ahead of
the FINAL recap.

Invariants locked here:
1. The CALLER HISTORY block renders after TRANSFER: and before FINAL.
2. The CUSTOMER CONTEXT block likewise (and after CALLER HISTORY).
3. Everything before the first per-caller block is byte-identical to the
   prompt built for a first-time caller with no customer context — i.e. the
   cross-call cacheable prefix is the whole tenant-stable prompt.
4. Two different repeat callers share that identical prefix.
5. A first-time caller's prompt has no per-caller block and still ends with
   the pinned FINAL line (recency placement unchanged).
6. Nothing but the FINAL recap follows the per-caller blocks.
"""
from __future__ import annotations

from src.prompt import build_system_prompt

CALLER_HISTORY_HDR = "CALLER HISTORY (silent context):"
CUSTOMER_CONTEXT_HDR = "CUSTOMER CONTEXT:"
FINAL_HDR = "FINAL — NON-NEGOTIABLES"
TRANSFER_HDR = "TRANSFER:"
PINNED_LAST_LINE = "Don't interrogate the caller about the situation."

_TENANT = dict(
    locale="en",
    business_name="Acme Plumbing",
    onboarding_complete=True,
    tone_preset="friendly",
    intake_questions="Is the water shut off?",
    country="US",
    tenant_timezone="America/Chicago",
)


def _history(name: str, n_interactions: int = 1) -> dict:
    return {
        "customer": {"name": name},
        "appointments": [],
        "interactions": [
            {"kind": "inquiry", "job_type": "leak", "status": "open"}
        ] * n_interactions,
        "tenant_timezone": "America/Chicago",
    }


_CUSTOMER_CTX = {"client": {"name": "Jane"}, "_sources": {"client": "Jobber"}}


def test_caller_history_block_trails_stable_sections():
    p = build_system_prompt(**_TENANT, caller_history=_history("Jane"))
    i_hist = p.index(CALLER_HISTORY_HDR)
    assert i_hist > p.index("BOOKING:")
    assert i_hist > p.index(TRANSFER_HDR)
    assert i_hist < p.index(FINAL_HDR)


def test_customer_context_block_trails_stable_sections():
    p = build_system_prompt(**_TENANT, customer_context=_CUSTOMER_CTX)
    i_ctx = p.index(CUSTOMER_CONTEXT_HDR)
    assert i_ctx > p.index(TRANSFER_HDR)
    assert i_ctx < p.index(FINAL_HDR)


def test_caller_history_precedes_customer_context_then_final():
    p = build_system_prompt(
        **_TENANT, caller_history=_history("Jane"), customer_context=_CUSTOMER_CTX
    )
    assert p.index(CALLER_HISTORY_HDR) < p.index(CUSTOMER_CONTEXT_HDR) < p.index(FINAL_HDR)


def test_repeat_caller_prefix_is_the_first_time_caller_prompt():
    base = build_system_prompt(**_TENANT, caller_history={})  # first-time caller
    repeat = build_system_prompt(**_TENANT, caller_history=_history("Jane"))
    prefix = repeat[: repeat.index(CALLER_HISTORY_HDR)]
    assert base.startswith(prefix)
    # The cacheable prefix is essentially the whole stable prompt — only the
    # short FINAL recap comes after the per-caller blocks.
    assert len(prefix) > 0.85 * len(base)


def test_two_repeat_callers_share_identical_prefix():
    a = build_system_prompt(**_TENANT, caller_history=_history("Jane", 1))
    b = build_system_prompt(**_TENANT, caller_history=_history("Bob", 3))
    assert a != b
    assert a[: a.index(CALLER_HISTORY_HDR)] == b[: b.index(CALLER_HISTORY_HDR)]


def test_first_time_caller_has_no_per_caller_block_and_recap_last():
    for hist in ({}, None):
        p = build_system_prompt(**_TENANT, caller_history=hist)
        assert CALLER_HISTORY_HDR not in p
        assert CUSTOMER_CONTEXT_HDR not in p
        assert p.rstrip().endswith(PINNED_LAST_LINE)


def test_only_final_recap_follows_per_caller_blocks():
    p = build_system_prompt(
        **_TENANT, caller_history=_history("Jane"), customer_context=_CUSTOMER_CTX
    )
    tail = p[p.index(CUSTOMER_CONTEXT_HDR):]
    assert tail.count("\n\n" + FINAL_HDR) == 1
    assert tail.rstrip().endswith(PINNED_LAST_LINE)
