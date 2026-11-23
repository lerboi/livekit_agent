# Voco voice agent — Project Instructions

This repo is the Python voice agent for Voco (LiveKit Agents 1.8, entrypoint `python -m src.agent`) plus a FastAPI webhook service. It deploys to Railway from `main`. The Next.js app, the database migrations and all project documentation live in the sibling repo `../homeservice_agent`.

## Project knowledge lives in the Obsidian vault

`../homeservice_agent/vault/` holds everything, as small linked notes. Nothing is documented in this repo except the dated plans in `docs/`.

| Need | Read |
|---|---|
| Orientation, current state | `../homeservice_agent/vault/Home.md`, `../homeservice_agent/vault/Now.md` |
| How this agent works | `../homeservice_agent/vault/Systems/Voice Agent/Voice Agent.md` (index) → only the notes you need |
| Booking and calendar push | `../homeservice_agent/vault/Systems/Scheduling and Calendar/Scheduling and Calendar.md` |
| Test-call sandbox and the demo console | `../homeservice_agent/vault/Systems/Demo Studio/Demo Studio.md` |
| Deploying, running a test call | `../homeservice_agent/vault/Runbooks/` |
| Why the pipeline is the way it is | `../homeservice_agent/vault/Decisions/` |
| Past work on this repo | `../homeservice_agent/vault/Log/` |

Comments in this code cite documents by old names. `findings.md` is `vault/Log/2026-06-11 Voice agent naturalness and information-collection findings (full report).md`; other moved files are listed in `vault/_meta/Moved files.md`. Phase numbers ("Phase 61 D-B2") refer to `../homeservice_agent/.planning/`.

## Rules

- Before changing the agent, read the Voice Agent index note. After changing it, update the matching vault note and set its `verified:` date.
- Run `pytest` before proposing a push. Never push to `main` without the owner's go-ahead: Railway deploys it to production.
- Test calls are sandboxed (`test_call` in room metadata): no calendar events, SMS, CRM rows, notifications or billing. Do not weaken those gates without an explicit decision recorded in the vault.
- No secrets in the vault or in this repo. Both GitHub repos are public.
