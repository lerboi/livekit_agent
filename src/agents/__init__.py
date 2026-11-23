"""Agents this worker can run, and the rule that picks one for a call.

One Railway service, one LiveKit worker name, several agents. ``src/agent.py``
is the worker's entrypoint; after it has connected and read the room metadata
it asks ``select_agent`` which agent should take the call:

* ``receptionist``: the original home-service receptionist code in
  ``src/agent.py`` (including its ``VOCO_AGENT_PROFILE`` scheduling profile).
* ``appointments``: the dedicated appointments agent in
  ``src/agents/appointments``: book, look up, move and cancel appointments in
  the business's connected calendar.
* ``demo``: the clinic front desk for the recorded product demo, in
  ``src/agents/demo``: its own prompt and turn policy on top of the
  appointments agent's tools and call wiring. Test calls only: it cannot be
  made the default.

Selection:

1. A test call whose server-set room metadata names an agent gets that agent.
   Room metadata is written by admin-gated routes; it is ignored on production
   calls, so nothing a caller controls can change which agent answers.
2. Everything else gets ``VOCO_DEFAULT_AGENT`` (default ``receptionist``, which
   is exactly the behaviour before this router existed).
"""

from __future__ import annotations

import os

RECEPTIONIST = "receptionist"
APPOINTMENTS = "appointments"
DEMO = "demo"
# Agents a production call may get (VOCO_DEFAULT_AGENT).
DEFAULT_AGENTS = (RECEPTIONIST, APPOINTMENTS)
# Agents a test call's server-set metadata may ask for.
AGENTS = DEFAULT_AGENTS + (DEMO,)


def default_agent() -> str:
    value = os.environ.get("VOCO_DEFAULT_AGENT", RECEPTIONIST).strip().lower()
    return value if value in DEFAULT_AGENTS else RECEPTIONIST


def select_agent(room_meta: dict | None, is_test_call: bool) -> str:
    if is_test_call and isinstance(room_meta, dict):
        requested = str(room_meta.get("agent") or "").strip().lower()
        if requested in AGENTS:
            return requested
    return default_agent()
