# Infra regions (P0.3 — 2026-09-04)

Phase 3 of the call-experience plan (region alignment) depends on these three
values. Fill in the two Railway-sourced ones the first time someone with
Railway access reads this; nothing else in Phase 0/1 depends on them.

| Component | Region | Source | Status |
|---|---|---|---|
| Supabase (`exbzhmparzjlpkryeiso`) | `ap-northeast-1` (Tokyo) | `SUPABASE_S3_REGION` in the agent `.env` | **verified 2026-09-04** |
| Railway agent service (`voco-voice-agent`) | _TO FILL_ | Railway → agent service → Settings → Region | needs dashboard/CLI access (the 2026-09-04 session had neither: `railway whoami` → Unauthorized, Chrome extension not connected) |
| LiveKit Cloud region the worker registers with (project `vocolive-*`) | _TO FILL_ | Railway deploy logs, the `registered worker` line — the SDK logs `region=` from `RegisterWorkerResponse.server_info.region` (`livekit/agents/worker.py` `_handle_register`) | same |

## How to fill the two blanks

```bash
railway login                                   # interactive, opens a browser
railway link                                    # pick the agent project/service
railway logs --lines 2000 | grep "registered worker"
```

The `registered worker` line carries `region=<livekit region>` (plus `url=` and
`protocol=`). The Railway service region is on the service's Settings page.

## Why it matters

Every Supabase round-trip from the worker is a Tokyo hop (~130 ms RTT from a US
worker; 4–5 sequential queries pre-greeting, 5–6 per booking). P1.10 keeps the
TLS session warm so only the RTT remains; Phase 3 (out of scope for now)
decides whether to move Supabase or the worker. Never move the worker to SG —
the vendors (OpenAI, Deepgram, ElevenLabs) are US-hosted.
