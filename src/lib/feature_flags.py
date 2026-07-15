"""Runtime feature flags for the Voco voice agent.

INTEGRATIONS_ENABLED — master kill-switch for the Jobber/Xero customer-context
integrations. v1 ships with this OFF so the integrations never touch the call
path: the pre-session merged-context fetch is skipped (no greeting-blocking
latency, no in-call token-death surface) and the ``check_customer_account`` tool
is not registered. The integration modules stay importable and dormant — do NOT
delete them (``tools.capture_lead`` depends on
``integrations.jobber._normalize_free_form``). Flip ``VOCO_INTEGRATIONS_ENABLED``
to ``true`` (Railway env) to re-enable.

See ``homeservice_agent/My Prompts/Jobber-Xero-Disable.md`` for the rationale and
the full re-enable checklist.

Fail-closed: OFF unless the env var is explicitly the string ``true``.
"""

import os

INTEGRATIONS_ENABLED: bool = (
    os.environ.get("VOCO_INTEGRATIONS_ENABLED", "false").strip().lower() == "true"
)
