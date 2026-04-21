# Phase 60 Sentry Regression Playbook

**Phase:** 60 — voice prompt polish (name-once + single-question address intake)
**Run these queries 24 hours after Railway deploy.**

These are manual Sentry queries — Phase 60 does not ship a Sentry alert rule.
Open Sentry → Issues or Discover, then paste each query below.

---

## Query 1 — `tool_call_cancelled` baseline check

```
tags.component:voice-agent AND message:"tool_call_cancelled" AND timestamp:[now-24h TO now]
```

**What to look for:**

The baseline is zero-to-few `tool_call_cancelled` events per day. This class of error was the
root cause of the Phase 999.2 VAD cutoff bug (resolved 2026-04-18 via `RealtimeInputConfig`
with LOW sensitivity, `prefix_padding_ms=400`, `silence_duration_ms=1000` in `agent.py`).

Phase 60 is a prompt-only change — `agent.py` is untouched. Any new spike in
`tool_call_cancelled` after Phase 60 deploys is a signal that a VAD-redundant guidance line
crept into `prompt.py` (e.g., "wait for the caller to finish", "don't interrupt", "let them
speak"). These phrases are known to correlate with the
`_SegmentSynchronizerImpl.playback_finished called before text/audio input is done` warning
that precedes cancelled tool calls.

**Action if spike found:** Check the Phase 60 `prompt.py` diff for any line matching:
- "wait for"
- "let the caller finish"
- "don't interrupt"
- "let them"
- "finish speaking"

Any such line should be removed — the VAD config in `agent.py` owns turn-taking entirely.

---

## Query 2 — Parrot-loop signal (tool return spoken verbatim)

```
tags.component:voice-agent AND (message:"Booking confirmed for" OR message:"slot is available" OR message:"earliest at" OR message:"lead captured") AND timestamp:[now-24h TO now]
```

**What to look for:**

Zero matches is the expected result. This query looks for the AI speaking one of the old
natural-English tool return strings verbatim — the "parrot loop" failure mode where Gemini
reads the return string directly to the caller instead of composing a natural response.

The canonical prior example of this bug: the `check_availability` tool once returned
"earliest slot at 9 AM, latest at 5 PM" — and Gemini spoke it word-for-word, fabricating
specific slot times to the caller. Phase 60's D-16 rewrites all tool returns to the strict
`STATE: ... | DIRECTIVE: ...` format to eliminate this class of error across all 5 tools.

Any match on this query after Phase 60 deploys means a tool return regressed to
natural-English format. Check the tool file involved (the string in the event body will
identify which tool), and rewrite the return to `STATE: ... | DIRECTIVE: ...` format.

**Zero matches = D-16 held. Any match = a tool return regressed and must be fixed.**

---

## Notes

- These queries cover the two primary regression signals for Phase 60. They are not
  exhaustive — review a sample of call transcripts from the first 24 hours post-deploy
  for qualitative checks (vocative name use, three-part address walkthrough, readback
  missing before booking tool fire).
- If you observe a new pattern not covered by these queries, add it here before the
  Phase 60 UAT sign-off.
- Phase 60 does not ship a Sentry alert rule or dashboard widget — these are manual
  reviewer queries for the UAT cycle only
