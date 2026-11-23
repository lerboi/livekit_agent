"""The demo agent: the front desk of a clinic, built for the recorded product demo.

It is a working agent, not a script. Nothing it says is written in advance and
nothing it reports is faked: it uses the appointments agent's real tools
(``src/agents/appointments/tools``), the real availability, the real
``appointments`` table and the real calendar write. What is its own:

* ``prompt.py``: a prompt written for one setting, a clinic's phone line, so the
  model is not also carrying rules for cases the demo never meets. It never
  asks what an appointment is for and books the moment the caller agrees.
* ``policy.py``: when the caller answers "yes" to a single time that is on
  offer, the turn must call the tool that makes the change. On a spoken take
  on 2026-11-24 the model answered "Yes, please" with "What is the appointment
  for?" and booked nothing.
* ``entry.py``: the settings for a demo call (this prompt, this policy,
  gpt-4.1-mini unless the Demo Studio names another model, speech-to-text
  keyterms for a clinic).

Everything else (session wiring, guards, recording, post-call) is
``src/agents/appointments/entry.run_call``, so the two cannot drift apart.

Selected only by the server-set room metadata of a test call
(``agent: "demo"``); it can never answer a production call.
"""
