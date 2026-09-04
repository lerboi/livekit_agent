# Voco voice agent — Phase 0 + Phase 1 execution handoff (2026-09-02)

This file is the complete brief for a fresh session. Everything below was verified against the working
tree of `C:/Users/leheh/.Projects/livekit-agent` (HEAD `b33c1d6`, branch `main`) and the installed SDK
`livekit-agents==1.5.7` at `C:/Users/leheh/AppData/Roaming/Python/Python313/site-packages/livekit/`
on 2026-09-02. Read it top to bottom before touching code. Line numbers are approximate (±5) — anchor on
the quoted strings.

The full four-phase plan (with baseline data, competitor research, and the latency budget) is published at
https://claude.ai/code/artifact/c6154045-8065-4935-b14e-555a9eb15689 . Phases 2–4 are OUT OF SCOPE here.

---

## 0. Why we are doing this

The receptionist is slow, sometimes goes silent, and feels robotic. Measured from Supabase (real calls,
`is_test_call is not true`):

- 71 of 141 real calls ever ended with the caller hanging up before any booking was attempted.
- 4 of the 8 real calls since the cascade pipeline launched (2026-06-09) hung up — all inside the address step.
- 76% of agent turns are questions; ~20 words per agent turn; 17–22 agent turns per successful booking.
- 14 of 21 address-validation attempts ever recorded returned `unconfirmed`.
- The literal filler "Let me just check that address real quick." was spoken 7× across 4 calls.
- No per-turn latency was ever measured until `b33c1d6` (2026-09-01) added `[agent] llm_metrics` lines.

Representative failure (real PSTN call `call-_+6587528516_urSJ7ZuUfKTv`, 2026-08-17, 142 s, no booking):
caller says "water leaking everywhere", agent asks for the address first, STT hears "Burr Drive" / "Kenboro
Drive" for **Canberra Drive**, the agent re-asks the block number four times (validate_address called 4×
in 66 s), caller hangs up. The postal code the caller gave (768433) was transcribed perfectly both times and
resolves via Singapore's free OneMap API to "BLK 40 CANBERRA DRIVE, YISHUN SAPPHIRE" — the agent never used
it. The same call was triaged `routine / low` because the Groq triage model was retired in July and the
failure is swallowed.

Root causes addressed by Phase 0 + 1: the address loop has no code-level cap (prose only), the prompt
teaches interrogation and repeats one literal filler, two SCHEDULING sentences contradict other rules and
cost a turn each, the greeting is 22 words spoken with caller input muted, every call pays a fresh TLS
handshake to a Supabase in Tokyo, the worker pool size is accidental, and two correctness bugs (triage
model, stale slot token) are live.

Decisions already made (do not reopen): keep OpenAI `gpt-4.1-mini` as the LLM (Groq/Cerebras only serve
open-weight reasoning models; TTFT to first *answer* token is worse). Keep the cascade (not speech-to-speech).
Prompt caching is already shipped (`b33c1d6`).

---

## 1. Ground rules (the functionality guarantee)

1. **Nothing the agent does today may be lost.** A prior audit inventoried 71 functional invariants in the
   prompt + tool DIRECTIVE strings (anti-hallucination of times/outcome words, verdict-token licence for
   "validated/verified", name-once policy, single-question address intake, booking readback, two-step
   `end_call`, EN+ES rules, out-of-area wording, caller-history privacy, section ordering). Many are pinned
   by substring tests. **Run the prompt tests after every prompt edit**; the pinned literals you must keep
   are listed per task below.
2. **Every runtime behaviour change ships behind a `VOCO_*` env flag** whose default is today's behaviour
   OR is a pure bug fix. Pattern to copy: `PREEMPTIVE_GENERATION` in `src/agent.py`.
3. **Tests:** `cd C:/Users/leheh/.Projects/livekit-agent && python -m pytest tests -q -p no:warnings`.
   Baseline on 2026-09-01: **535 passed, 1 failed** — the failure is
   `tests/webhook/test_routes.py::test_incoming_call_vip_lead`, pre-existing for months. Any other failure
   is yours.
4. **Do not** remove/rename/reorder tools, change STATE/DIRECTIVE token names, touch notifications, billing,
   subscription gate, recording, Spanish handling, or the prompt-cache layout (per-caller sections must stay
   after TRANSFER and before FINAL — `tests/test_prompt_cache_prefix.py`).
5. **Never add `if locale == "es"` branches** in prompt section builders (single-English-prompt policy;
   `tests/test_prompt_locale_collapse.py`). Country branches (`country == "SG"`) are fine.
6. **Skill sync:** after code lands, prepend a dated entry to the `**Last updated**:` line of
   `C:/Users/leheh/.Projects/homeservice_agent/.claude/skills/voice-call-architecture/SKILL.md` and update the
   affected sections (§1 cascade construction, §4 prompt, §5 tools, §7 triage, §11 env vars).
7. **Commits:** one commit per task below, on `main` in each repo (that is this project's convention;
   Railway deploys the agent from `main`). Commit only when asked. End commit messages with:
   `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
8. Never print secret values from `.env` files.

---

## 2. Repo map you need

Agent repo (`C:/Users/leheh/.Projects/livekit-agent`):
- `src/agent.py` — entrypoint; LLM construction (`_llm_kwargs = dict(...)` → `openai.LLM(**_llm_kwargs)`),
  `@llm.on("metrics_collected")`, `_warm_prompt_cache`, `on_conversation_item` (transcript capture),
  greeting via `session.say(greeting_text, ...)`, `WorkerOptions(...)` at the bottom, boot preflight
  `_missing_keys = [...]` in `__main__`.
- `src/prompt.py` — `build_system_prompt(...)`; section builders `_build_*_section`. Sections joined with
  `"\n\n"`. `postal_label = "postal code" if country == "SG" else "zip code"`.
- `src/tools/validate_address.py`, `check_slot.py`, `check_day.py`, `next_available_days.py`,
  `book_appointment.py`, `capture_lead.py`, `check_caller_history.py`, `transfer_call.py`, `end_call.py`,
  `_availability_lib.py` (`register_slot_token`, `pick_spread`).
- `src/integrations/google_maps.py` — `validate_address_with_region_fallback(...)`, `validate_address_bounded`,
  `map_components` / `_empty_components()` (named-key `address_components` shape), `_apply_country_guard`.
- `src/lib/triage/layer2_llm.py` — Groq classifier (`run_llm_scorer`).
- `src/post_call.py` — post-call pipeline; builds `transcript_structured`.
- `src/supabase_client.py` — `get_supabase_admin()` (lazy module global).
- `src/messages/en.json`, `es.json` — deterministic greeting + recovery strings (`agent.*` keys).
- `tests/` — 49 files; prompt tests are `tests/test_prompt*.py`.

Main repo (`C:/Users/leheh/.Projects/homeservice_agent`): only the skill file above is touched in this phase.
Supabase project `exbzhmparzjlpkryeiso` (MCP tools `mcp__supabase-voco__*`) is available for read queries.

Installed-SDK facts (verified):
- `ChatMessage.metrics` is a `TypedDict(total=False)` `MetricsReport` with keys `started_speaking_at`,
  `stopped_speaking_at`, `transcription_delay`, `end_of_turn_delay`, `on_user_turn_completed_delay` (user
  messages) and `llm_node_ttft`, `tts_node_ttfb`, `playback_latency` (assistant messages). Use `.get()`.
  (`agents/llm/chat_context.py` ~L260-300.) `AgentSession.on("metrics_collected")` is deprecated; the
  LLM-object event already used in `agent.py` is not.
- `WorkerOptions(num_idle_processes=...)` exists (`agents/worker.py` ~L205; prod default `min(ceil(cpu),4)`).
- `supabase==2.28.3`: `supabase.lib.client_options.SyncClientOptions` has an `httpx_client` field.
  postgrest already uses `http2=True` (so `h2` is installed). httpx default `keepalive_expiry` is 5.0 s.
- `appointments.address_validation_verdict` / `inquiries.address_validation_verdict` CHECK allows exactly
  `confirmed, confirmed_with_changes, unconfirmed, error, skipped, unsupported_region`.
- Deepgram plugin defaults the code inherits: `endpointing_ms=25`, `interim_results=True`, `no_delay=True`,
  `smart_format=False`, `numerals=False` (digits arrive as words; the LLM converts them for tool args).
- OneMap (Singapore Land Authority) search, no auth, verified live 2026-09-01:
  `GET https://www.onemap.gov.sg/api/common/elastic/search?searchVal=768433&returnGeom=N&getAddrDetails=Y&pageNum=1`
  → `{"found":1,"results":[{"SEARCHVAL":"YISHUN SAPPHIRE","BLK_NO":"40","ROAD_NAME":"CANBERRA DRIVE",
  "BUILDING":"YISHUN SAPPHIRE","ADDRESS":"40 CANBERRA DRIVE YISHUN SAPPHIRE SINGAPORE 768433","POSTAL":"768433"}]}`.
  `BUILDING` is `"NIL"` for landed/private houses.

---

## 3. PHASE 0 — measure (do first, ~half a day)

### P0.1 Per-turn stage timings in the existing transcript handler
- **Where:** `src/agent.py`, `def on_conversation_item(event):` (search `"timestamp": int(time.time() * 1000)`).
- **Change:** after the `transcript_turns.append(...)`, read `m = getattr(event.item, "metrics", None) or {}`
  and log one line:
  `logger.info("[agent] turn_metrics call=%s role=%s eot_delay=%s transcription_delay=%s llm_ttft=%s tts_ttfb=%s",
  call_id, role, m.get("end_of_turn_delay"), m.get("transcription_delay"), m.get("llm_node_ttft"), m.get("tts_node_ttfb"))`.
  Wrap in try/except; never let logging break the handler.
- **Why:** these four numbers are the stage attribution the whole plan is measured with. `end_of_turn_delay`
  is the endpointing wait we intend to cut; `llm_node_ttft` is exposed LLM time.
- **Test:** none required (logging). Keep the handler's existing behaviour byte-identical otherwise.

### P0.2 Keep per-turn timestamps in the stored transcript
- **Where:** `src/post_call.py`, the block
  `transcript_structured = [{"role": t["role"], "content": t["content"]} for t in transcript_turns]`.
- **Change:** add `"timestamp": t.get("timestamp")` to each dict. `transcript_turns` already carries it
  (`agent.py` `on_conversation_item`).
- **Why:** makes caller-turn-end → agent-turn-start gaps computable for every future call via SQL.
- **Compatibility:** `calls.transcript_structured` is `jsonb`. Main-repo consumers (`src/app/api/calls/route.js`,
  `src/app/dashboard/calls/page.js`, `src/components/dashboard/InquiryFlyout.jsx`, `JobFlyout.jsx`,
  `src/lib/jobs.js`, `src/app/admin/test-agent/page.js`, `src/app/api/admin/test-agent/result/route.js`)
  read `role`/`content`; grep them for strict shape checks before assuming — adding a key is additive.
- **Test:** if a test asserts the exact dict shape in `tests/test_post_call*.py`, extend it; do not weaken it.
- **Query to keep in the doc:**
  ```sql
  with t as (select c.id, (x->>'role') role, (x->>'timestamp')::bigint ts, ord
             from calls c, jsonb_array_elements(c.transcript_structured) with ordinality as e(x, ord)
             where c.created_at > '2026-09-02')
  select id, avg(gap_ms) avg_gap_ms, percentile_cont(0.95) within group (order by gap_ms) p95_gap_ms from (
    select a.id, b.ts - a.ts gap_ms from t a join t b on b.id=a.id and b.ord=a.ord+1
    where a.role='user' and b.role='agent') g group by id;
  ```

### P0.3 Confirm regions (no code)
- Railway → agent service → Settings → Region (write it down).
- Railway logs: grep `registered worker` — the SDK logs `region=` for the LiveKit region the worker is
  registered with (`agents/worker.py` ~L1197-1208).
- Supabase is `ap-northeast-1` (Tokyo) — confirmed from `SUPABASE_S3_REGION` in the agent `.env`.
- Record the three in a short `docs/INFRA-REGIONS.md`. Phase 3 depends on this.

### P0.4 Baseline before Phase 1 ships
- After P0.1/P0.2 are deployed, make 3–5 test-console calls (admin `/admin/test-agent`, rooms
  `test-web-*`, all side effects sandboxed) and record: `turn_metrics` medians, the SQL gap query, agent
  turns per booking, words per agent turn. Paste them into this file under "Baseline (measured)".

---

## 4. PHASE 1 — stop the bleeding (this week)

Order matters only where noted. Each task = one commit.

### P1.1 Move the triage layer-2 classifier off Groq to OpenAI gpt-4.1-nano (correctness — do first)
- **Where:** `src/lib/triage/layer2_llm.py` → `_get_client()` builds `AsyncOpenAI(api_key=GROQ_API_KEY,
  base_url="https://api.groq.com/openai/v1")` and `run_llm_scorer` pins
  `model="meta-llama/llama-4-scout-17b-16e-instruct"`.
- **Facts (verified 2026-09-04 against console.groq.com/docs/deprecations):** Groq shut down
  `llama-4-scout-17b-16e-instruct` on 2026-07-17 and BOTH `llama-3.3-70b-versatile` and `llama-3.1-8b-instant`
  on 2026-08-16. Groq's remaining production LLMs (`openai/gpt-oss-120b`, `openai/gpt-oss-20b`) are reasoning
  models — with `max_tokens=100` their reasoning tokens consume the budget → empty content → JSON error → the
  same silent `routine/low` fallback. So Groq is no longer a fit for this classifier at all. The blanket
  `except Exception: return {"urgency": "routine", "confidence": "low", ...}` has hidden the outage since
  mid-July. `GROQ_API_KEY` is not in the boot preflight.
- **Replacement:** OpenAI `gpt-4.1-nano` — current (no deprecation notice), non-reasoning ("low latency without
  a reasoning step"), $0.10 / $0.40 per 1M tokens ($0.025 cached), supports `response_format`/structured outputs,
  and uses `OPENAI_API_KEY`, which is ALREADY a fatal boot-preflight requirement — the missing-key gap closes by
  construction. A transcript classification is ~1–2k input tokens → well under $0.001 per call; TTFT ~0.4 s +
  100 output tokens fits the 2.5 s timeout with margin.
- **Change:**
  1. `_get_client()`: `AsyncOpenAI()` with no `base_url` and no explicit `api_key` (the SDK reads
     `OPENAI_API_KEY`). Remove the Groq base_url. Keep it a lazily created module global.
  2. `run_llm_scorer`: `model="gpt-4.1-nano"`. Keep `response_format={"type": "json_object"}` (the SYSTEM_PROMPT
     already contains the word "JSON", which json_object mode requires), `max_tokens=100`, `temperature=0`, and
     the 2.5 s `asyncio.wait_for`. Optionally lift the model id into a module constant `LAYER2_MODEL`.
  3. Split the except: `except asyncio.TimeoutError: logger.warning("[triage] layer2 timeout")` and
     `except Exception as e: logger.error("[triage] layer2 failed: %s", e)` — both still return the fallback dict
     (the post-call pipeline must never raise). Add `import logging; logger = logging.getLogger(__name__)`.
  4. No preflight change needed. `GROQ_API_KEY` becomes unused by the agent — note it in the skill's §11 env
     table as "unused since 2026-09 (layer-2 triage moved to OpenAI gpt-4.1-nano)"; leave the Railway var in
     place for now.
- **Tests:** `tests/test_triage_layer1_keywords.py` monkeypatches `run_llm_scorer`; no test pins the model
  id or the base_url. Add `tests/test_triage_layer2.py`: (a) a raised exception inside the OpenAI call still
  returns the fallback dict; (b) a timeout returns the fallback dict; (c) `LAYER2_MODEL == "gpt-4.1-nano"` and
  the client's `base_url` host is `api.openai.com`; (d) a valid JSON response is parsed and returned as-is.
- **Verify live after deploy:** `select urgency_classification, urgency_confidence, triage_layer_used from
  calls order by created_at desc limit 5` after a test call saying "water is flooding my kitchen right now" —
  expect `emergency` / `high` (or `urgent`), not `routine / low`.

### P1.2 Stale slot token — one-line fix in check_day (correctness)
- **Where:** `src/tools/check_day.py`, inside the tool body, right before the options loop that calls
  `register_slot_token(deps, slot["start"], slot["end"])` (the `day_has_slots` branch) — or simply once, after
  argument parsing and before any return.
- **Change:** `deps.pop("_last_offered_token", None)`.
- **Why:** `_last_offered_token` is written only by `check_slot` and cleared only by `check_slot`'s
  alternatives branch and `book_appointment` success; `check_day` never touched it. Sequence
  `check_slot(Tuesday)` → caller switches → `check_day(Wednesday)` → model emits a malformed token →
  `book_appointment` "recovers" with Tuesday's stale token and overrides `slot_start/slot_end` with Tuesday's
  UTC values, returning `BOOKED` — the agent then confirms Wednesday aloud.
- **Do NOT change `book_appointment`'s recovery branch.** `tests/test_slot_token_handoff.py::
  test_hallucinated_token_falls_back_to_last_offered` and `::test_no_token_falls_back_to_last_offered` pin
  it, and it is correct when the last offer is fresh. Clearing the stale value in `check_day` is sufficient.
- **Tests:** add to `tests/test_availability_alternatives.py` (or a new file): set
  `deps["_last_offered_token"]="stale"`, run `check_day`, assert the key is gone. Existing asserts
  (`deps["_last_offered_token"] in deps["_slot_tokens"]`) are for `check_slot` paths — unaffected.

### P1.3 Cap the address loop in code
- **Where:** `src/tools/validate_address.py`, inside `validate_address(raw_arguments, context)`, the
  `elif verdict == "unconfirmed":` branch that builds `STATE:address_unclear missing=...`.
- **Change:**
  1. Before the Google call, count attempts per normalized street:
     `attempts = deps.setdefault("_validate_attempts", {}); key = _norm(street) or "_"; attempts[key] = attempts.get(key, 0) + 1`.
  2. In the `unconfirmed` branch: if `attempts[key] >= 2`, return the `address_noted` state instead:
     `f"STATE:address_noted speech={as_given} | DIRECTIVE:read it back once in the caller's words and continue with the next intake step. Do not ask about the address again this call. Never mention validation."`
     (First unconfirmed attempt keeps today's `address_unclear` return — `tests/test_validate_address_tool.py::
     test_unconfirmed_returns_address_unclear_with_hint` and `::test_gate_does_not_fire_on_unconfirmed` call
     the tool once with a fresh `_make_deps()` and must still pass.)
  3. Keep `deps["_validated_address"]` caching and `deps["_last_tool_state"]` exactly as today.
- **Why:** the DIRECTIVE "After one retry, proceed with what the caller said" is prose; under
  last-instruction-wins the identical `address_unclear` return re-arrives and the model asks again (4× in 66 s
  on the August call). The tool must enforce the cap.
- **Prompt companion edit** (`src/prompt.py`, `_build_address_validation_section`, the `STATE:address_unclear`
  bullet): "ask for just the piece named in `missing=` as a plain question (\"What's the block number there?\"),
  then call validate_address again. The tool switches to `address_noted` when it is time to stop — never ask about
  the same piece twice." Keep the literals `STATE:address_unclear`, `STATE:address_noted`, the phrase
  "more than twice", "never leave the line silent", and do not introduce the phrase "silence is acceptable" in
  this section (`tests/test_prompt_address_validation_rule.py`).
- **Tests:** new test: two consecutive `unconfirmed` results with the same street → second return starts with
  `STATE:address_noted` and contains "Do not ask about the address again".

### P1.4 Singapore: resolve the postal code with OneMap before Google
- **Where:** `src/tools/validate_address.py` (before `validate_address_with_region_fallback(...)`) + a new
  `src/integrations/onemap.py`.
- **Change:**
  1. `onemap.py`: `async def lookup_postal(postal: str, *, timeout_seconds: float = 1.5) -> dict | None` —
     `httpx.AsyncClient` per call (same pattern as `google_maps.py`), GET the URL in §2, return the first result
     dict or `None`; **never raises** (catch everything, log warning). Only call when `re.fullmatch(r"\d{6}", postal)`.
  2. In the tool: `if region_code == "SG" and re.fullmatch(r"\d{6}", postal_code) and ONEMAP_ENABLED:` call
     `lookup_postal`. On a hit, build a `result` dict in the exact shape `google_maps.py` returns (read
     `map_components` / `_empty_components()` for the named keys and fill `street_number=BLK_NO`,
     `route=ROAD_NAME` title-cased, `postal_code=POSTAL`, `locality="Singapore"`, `country_code="SG"`;
     `formatted_address = f"Block {BLK_NO} {Road Name}[, {Building}]"` with Building omitted when `"NIL"`;
     `verdict = "confirmed"` if the caller's spoken street contains the block number or a fuzzy match of the road
     name, else `"confirmed_with_changes"` (this is the Canberra/Kenboro case → the agent reads back the
     corrected form once and asks — the existing `address_corrected` branch already does exactly that).
     Then fall through to the existing STATE building code unchanged. On no hit → today's Google path.
  3. Flag: `ONEMAP_ENABLED = os.environ.get("VOCO_SG_ONEMAP", "true").strip().lower() != "false"` in the tool
     module (or `agent.py` if you prefer all flags together — then pass via `deps`).
  4. Telemetry: log `[validate_address] onemap hit postal=… blk=… road=…`. Do NOT write rows to
     `gmaps_validate_events` (that table is Google-specific).
- **Why:** SG postal codes identify a single building; STT transcribes digits reliably and proper-noun street
  names unreliably. Both August hang-ups would have resolved on the first attempt.
- **Prompt companion edit** (`_build_info_gathering_section`, SERVICE ADDRESS block): add a country-gated
  sentence when `country == "SG"` — pass `country` into the builder (add a kwarg with default `"US"` so positional
  test calls keep working): "In Singapore the postal code pins down the building — if the caller gives it, call
  validate_address right away even if the street name was unclear; then ask only for the unit number." Keep
  the pinned words "one", "at a time", "address", "phone number", "read back"/"confirm", and the postal label
  (`tests/test_prompt_info_gathering.py`, `tests/test_prompt_booking.py`).
- **Tests:** new `tests/test_onemap.py` (mock `httpx.AsyncClient.get`: hit, miss, timeout, HTTP 500 → never
  raises) and validate_address tests for `region_code="SG"` + 6-digit postal → `STATE:address_corrected` when
  the spoken street differs, `STATE:address_ok` when it matches; non-6-digit or non-SG → Google path called
  (assert the patched Google mock was awaited).
- **Also:** the service-area gate reads `address_components.postal_code`/`.locality` — with the fields above
  it keeps working for SG zones.

### P1.5 Endpointing max 2.0 → 1.2 s (Railway env, no deploy)
- Set `VOCO_MAX_ENDPOINTING_DELAY_S=1.2` on the Railway service. Watch test calls reading a full address
  with natural pauses; if the agent jumps in, use 1.4. Do not lower `VOCO_MIN_ENDPOINTING_DELAY_S` (0.4).
- Update the default in `src/agent.py` (`MAX_ENDPOINTING_DELAY_S = float(os.environ.get(..., "2.0"))`) to
  `"1.2"` only after the env value has run for a few days without talk-over.

### P1.6 Rotating, tool-keyed filler bank
- **Where:** `src/prompt.py` `_build_tool_narration_section` + the tool descriptions that quote a literal
  filler: `check_slot.py` ("'Let me pull that up real quick'"), `check_day.py` ("'Let me see what that day looks
  like'"), `next_available_days.py` ("'Let me see what's …'"), `book_appointment.py` ("'Let me get that booked
  in for you'"), `validate_address.py` `_SCHEMA["description"]` ("'Let me just check that address…'").
- **Change (tool descriptions):** replace each quoted literal with "speak one short, varied filler first (never the
  same one twice in a call — see TOOL NARRATION), then invoke in the same turn". GPT-4.1 copies the literal
  nearest the decision point; the tool description is that point.
- **Change (prompt section):** keep rules 1–6; add rule 7 "Never say the same filler twice in one call — rotate
  and change the shape, not just a word" and rule 8 "A second call of the same tool inside one loop (a second
  validate_address, a second check_slot) takes a short bridge on your acknowledgement instead of a fresh filler:
  'Okay, seven six eight — trying that.'"; replace the example bank with 3–4 differently shaped phrases per tool
  (imperative, first-person, question-tag) and drop the "one moment" tail from most of them. Drop the closing
  "second-worst … worst" sentence.
- **Pinned literals to KEEP** (`tests/test_prompt_tool_narration.py`, `tests/test_prompt.py`): lowercase text
  must contain "never emit a tool call without speaking first"; must contain one of "~2 second" / "2 seconds" /
  "one warm sentence"; must contain every tool name `check_slot, check_day, next_available_days, validate_address,
  book_appointment, capture_lead, transfer_call`; must NOT contain "longer, warmer filler", "runtime", "session.say";
  EN and ES outputs identical; length > 200.
- **Tests:** run `tests/test_prompt*.py`; add an assertion that no example phrase appears twice in the section.

### P1.7 Acknowledge + act (stop restating the caller's problem)
- **Where:** `src/prompt.py` `_build_voice_behavior_section`, the paragraph beginning
  "After the caller answers, acknowledge in a few words at most"; and `_build_info_gathering_section` NAME USE
  bullet "- The acknowledgment outcome is to confirm receipt without using the caller's name."
- **Change:** VOICE paragraph → "After the caller answers, act: move straight to the next step, or lead with at
  most two words (\"Okay.\" \"Sure.\") and then act. Never restate what the caller just said — the only things you
  read back are a name, an address, a phone number, or a booking time. WRONG: \"Got it, your toilet needs
  fixing.\" RIGHT: \"Okay — what's the address?\" Vary your openers; never start two turns in a row the same way."
  NAME USE bullet → "An acknowledgment, when you use one, is two words at most and must not contain the caller's
  name."
- **Pinned literals to KEEP:** "match the caller's energy", "slow down", "addresses", "dates",
  "VOICE & CONVERSATION STYLE:" (`tests/test_prompt_voice_behavior.py`); "must not contain the caller's name",
  "Forbidden patterns", "Thanks, {name}", "Thank you, {name}", "{name}, I have", "SOLE moment"
  (`tests/test_prompt_name_and_language_hardening.py`).

### P1.8 Fix the two SCHEDULING contradictions
- **Where:** `src/prompt.py` `_build_booking_section`, the "SCHEDULING:" paragraph.
- **Today:** "Only discuss scheduling once you have the caller's name, their issue, and a confirmed address. …
  Scheduling needs both a day and a time; if they give you one, help them decide the other before you check."
  Contradicts "Booking is never blocked by a missing name" (NAME USE) and "date but no time → call check_day"
  (AVAILABILITY RULES routing) — each costs an extra question turn per booking.
- **Change:** "SCHEDULING:\nMove to scheduling once you have the issue and a confirmed address — get the name on
  the way if you can, but never hold booking for it. Appointments are only for upcoming dates and times; if the
  caller names a past date or a time too soon, say so and guide them to something workable. Pick the tool by what
  the caller gave you: a day and a time → check_slot now; a day only → check_day now (don't ask \"what time?\"
  first — offer what it returns); nothing specific → next_available_days now."
- **Pinned literals to KEEP** (`tests/test_prompt_booking.py`, lowercase): "check_slot", "check_day",
  "next_available_days", "before", "book", "book_appointment", ("read back" or "confirm"), "address",
  ("confirmed" or "booked"), plus `business_name` and the postal label appearing in the section. The word
  "before" must remain somewhere in the section (e.g. "check_slot before book_appointment" in the existing rules).

### P1.9 Shorter greeting (disclosure kept, one clause)
- **Where:** `src/messages/en.json` + `src/messages/es.json`, keys `agent.greeting_onboarding`,
  `agent.greeting_default`. Composition happens in `src/agent.py` (`greeting_text = _msg(locale,
  "agent.greeting_onboarding").format(business_name=business_name)`).
- **Change (EN):** `greeting_onboarding` → "Thanks for calling {business_name}. Calls are recorded — how can I
  help?"; `greeting_default` → "Hi, thanks for calling. Calls are recorded — how can I help?".
  **(ES, with diacritics):** "Gracias por llamar a {business_name}. La llamada se graba — ¿en qué puedo ayudarle?"
  and "Hola, gracias por llamar. La llamada se graba — ¿en qué puedo ayudarle?". While there, add diacritics
  and inverted punctuation to the other ES `agent.*` strings (ElevenLabs prosody improves) and fix
  `recovery_error` so it no longer promises "Let me take your details" — the code path speaks it and hangs up;
  use "Sorry — the line's breaking up on my end. Someone from the team will call you right back at this number."
- **Keep:** the `agent.recording_disclosure` key (a test reads it to assert the disclosure is NOT inlined in the
  prompt: `tests/test_prompt_greeting_directive.py`, `tests/test_prompt_tail_sections.py`). No test pins the
  greeting strings themselves.
- **Why:** 22 words → 12, spoken non-interruptible with caller input muted; the recording clause stays (US
  two-party states; SG PDPA purpose notice) — Upfirst's 450k-call data shows it lowers hang-ups.
- **Owner decision noted:** a per-tenant `off/short/full` disclosure mode is Phase 4; not now.

### P1.10 Supabase keep-alive + warm client in prewarm
- **Where:** `src/supabase_client.py` and `prewarm(proc)` in `src/agent.py`.
- **Change:**
  ```python
  import httpx
  from supabase import create_client, Client
  from supabase.lib.client_options import SyncClientOptions
  _http = httpx.Client(http2=True, timeout=httpx.Timeout(10.0), limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=None))
  _supabase = create_client(url, key, options=SyncClientOptions(httpx_client=_http))
  ```
  and in `prewarm(proc)` (agent.py): `try: get_supabase_admin().table("tenants").select("id").limit(1).execute() except Exception as e: logger.warning(...)`
  so each idle job process holds an open TLS session to Supabase before a call arrives.
- **Why:** job processes are forked; the module-global client is built on first use per process, and httpx's
  default 5 s keep-alive expiry re-handshakes between tool calls. Supabase is in Tokyo (~130 ms RTT from the US
  worker); a TLS handshake there is ~250–350 ms on the first query of every call.
- **Check:** `SyncClientOptions(httpx_client=...)` is honoured by postgrest/storage/auth sub-clients in
  supabase 2.28.3 — read `supabase/_sync/client.py` to confirm the field name and that a `verify`/`timeout`
  isn't overridden. Any test constructing `get_supabase_admin()` with mocked env must still pass.

### P1.11 Pin the worker pool
- **Where:** `src/agent.py` `WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm, agent_name="voco-voice-agent")`.
- **Change:** add `num_idle_processes=int(os.environ.get("VOCO_NUM_IDLE_PROCESSES", "2"))`.
- **Why:** today the pool is `min(ceil(cpu_count), 4)` by accident; a cold job process (interpreter + imports +
  VAD load) is 2–5 s of dead air. Two warm processes ≈ 300–500 MB RAM. Watch Railway logs for
  "no warmed process available for job" to tune.

---

## 5. Suggested order and commit grouping

1. P0.1 + P0.2 (one commit: "feat: per-turn stage metrics + keep transcript timestamps") → deploy → P0.3/P0.4.
2. P1.1, P1.2 (two commits, correctness) → deploy.
3. P1.3 + P1.4 (address loop cap + OneMap; one commit each) with new tests.
4. P1.6 + P1.7 + P1.8 + P1.9 (prompt + messages; one commit "feat: prompt naturalness pass — rotating fillers,
   acknowledge+act, scheduling fix, shorter greeting") — run ALL prompt tests, then 3 EN + 3 ES test-console calls.
5. P1.10 + P1.11 (one commit) → deploy → check Railway logs for the prewarm ping and pool warnings.
6. P1.5 is an env change on Railway; record it in the skill's §11 table.
7. Skill sync commit in the main repo.

After each deploy, make one test-console call and read: `[agent] turn_metrics`, `[agent] llm_metrics …
cached_tokens=`, `[agent] prompt cache warmed`, and (P1.4) `[validate_address] onemap hit`.

---

## 6. Acceptance for Phase 0 + 1

- Suite: everything green except the pre-existing `test_incoming_call_vip_lead`.
- A test call reading "40 Canberra Drive, 768433, unit 07-04" resolves the address in ≤ 2 agent turns.
- No agent line (other than greeting/goodbye) repeats verbatim within a call (`transcript_structured`).
- `turn_metrics` shows `end_of_turn_delay` ≤ 1.2 s on address turns; `cached_tokens` ≈ prompt size on turn 1.
- A test call saying "water is flooding my kitchen right now" is triaged `emergency` (not `routine/low`).
- Greeting audio ≤ 3 s.

---

## 7. Out of scope (Phase 2–4 — do not start)

Deepgram Flux STT + `turn_detection="stt"`, `preemptive_tts` / `TurnHandlingOptions` migration, adaptive
interruption, background/thinking audio, greet-then-`update_instructions`, cached greeting audio, LLM
`FallbackAdapter`, single webhook RPC, region alignment, Supabase migration, SDK upgrade, intake reorder,
emergency branch, prompt consolidation to ~4.8k tokens, per-tenant disclosure mode, SMS address fallback.

---

## 8. Execution status (2026-09-04)

Implemented in the working tree (uncommitted — commits are made on request, one per task per §5):

| Task | Status | Notes |
|---|---|---|
| P0.1 turn_metrics log | done | `agent.py` `on_conversation_item`; try/except-wrapped |
| P0.2 transcript timestamps | done | `post_call.py`; additive `timestamp` key |
| P0.3 regions | partial | `docs/INFRA-REGIONS.md` — Supabase verified; Railway + LiveKit region TO-FILL (no Railway CLI login / Chrome extension in the session) |
| P0.4 baseline calls | **not done** | needs a deploy + test-console calls |
| P1.1 triage → gpt-4.1-nano | done | `tests/test_triage_layer2.py`. Follow-up from live timing: nano is ~0.85 s warm but the cold first request (~2.1 s incl. TLS) tripped the 2.5 s cap, and every call's triage runs in a fresh job process → `layer2_llm.warm_client()` fired at call start (`VOCO_TRIAGE_LAYER2_WARM`), keep-alive expiry disabled on the client, `VOCO_TRIAGE_LAYER2_TIMEOUT_S` env-tunable, `[triage] layer2 ok … elapsed_ms=` logged |
| P1.2 stale slot token | done | `check_day` pops `_last_offered_token` on entry |
| P1.3 address-loop cap | done | per-street cap 2 **+ call-level backstop 3** (deviation: STT drift of the street string would otherwise reset the per-street clock) |
| P1.4 OneMap SG postal-first | done | `src/integrations/onemap.py`; `returnGeom=Y` (lat/lng filled — deviation from the `N` in §2); spaces/hyphens stripped before the 6-digit check; verified live for 768433 |
| P1.5 endpointing 1.2 s | **not done** | Railway env change — no access this session |
| P1.6 filler bank | done | shapes used: Let me… / I'll… / Hang on… / participle — no question-tag examples (deviation: a tag invites a reply while the tool runs); ADDRESS VALIDATION opener's literal filler also removed |
| P1.7 acknowledge + act | done | |
| P1.8 SCHEDULING fix | done | |
| P1.9 shorter greeting | done | EN/ES + ES diacritics + `recovery_error` reword; `tests/test_messages_greeting.py` |
| P1.10 Supabase keep-alive | done | flags `VOCO_SUPABASE_KEEPALIVE` / `VOCO_SUPABASE_TIMEOUT_S` (10 s; postgrest default was 120 s) / `VOCO_SUPABASE_PREWARM`; live: 204 ms after 7 s idle vs 532 ms plain |
| P1.11 worker pool | done | `VOCO_NUM_IDLE_PROCESSES` default 2 |

Suite after all tasks: **614 passed / 1 failed** (the pre-existing `test_incoming_call_vip_lead`).

Flags whose default is NOT today's behaviour (per this brief's task specs): `VOCO_SG_ONEMAP=true`,
`VOCO_SUPABASE_KEEPALIVE=true`, `VOCO_SUPABASE_PREWARM=true`, `VOCO_NUM_IDLE_PROCESSES=2`. Flip any of them
on Railway before deploying if a conservative first rollout is preferred.

### Baseline (measured)

_Pending P0.4 — fill after the first deploy (turn_metrics medians, the §3 SQL gap query, agent turns per
booking, words per agent turn)._
