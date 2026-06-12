"""Shared subscription gate (2026-06-12 audit H1).

Single source of truth for which subscription states block inbound calls —
previously BLOCKED_STATUSES was hand-copied in agent.py and
webhook/twilio_routes.py (and mirrored in the main repo's
src/lib/subscription-gate.js), which is exactly how the past_due gap survived:
the 3-day grace period (Phase 25 D-05) existed only as banner copy, no code
ever enforced its END, and a tenant whose payment failed kept the AI answering
calls forever.

Rules:
- canceled / paused / incomplete  -> always blocked.
- past_due                        -> blocked once the 3-day grace period after
                                     current_period_end has elapsed.
- anything else (active/trialing) -> allowed.
- missing/unparseable period end  -> past_due stays allowed (fail open, same
                                     error posture as the rest of the gate).
"""

from datetime import datetime, timedelta, timezone

BLOCKED_STATUSES = ["canceled", "paused", "incomplete"]

# Keep in sync with the main repo: BillingWarningBanner GRACE_PERIOD_MS and
# subscription-gate.js both document 3 days.
PAST_DUE_GRACE = timedelta(days=3)


def is_subscription_blocked(status, current_period_end=None) -> bool:
    """Return True when an inbound call must be refused for this subscription.

    `current_period_end` is the subscriptions row value (ISO-8601 string or
    None) — the grace window is anchored to the end of the billing cycle that
    failed to collect, matching calculateGraceDaysRemaining in the dashboard
    banner.
    """
    if status in BLOCKED_STATUSES:
        return True

    if status == "past_due":
        if not current_period_end:
            return False
        try:
            end = datetime.fromisoformat(str(current_period_end).replace("Z", "+00:00"))
        except ValueError:
            return False
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > end + PAST_DUE_GRACE

    return False
