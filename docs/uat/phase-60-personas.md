# Phase 60 UAT Personas — Voice Prompt Polish

**Phase:** 60 — name-once + single-question address intake
**Decisions covered:** D-01 through D-14
**Run these personas on a live Railway call after deploy. All persona tests are manual.**

---

## Persona 1 — Culturally diverse name + clear address (baseline)

**Addresses D-IDs:** D-01, D-02, D-06
**Language:** en

**Script (what the tester says):**

1. "Hi, my name is Jia En Tan. I need a plumber at 42 Lornie Road, Singapore 298107."
2. (Follow along naturally — answer any questions the AI asks.)
3. (If the AI reads back details, confirm them or correct as appropriate.)
4. (Accept the booking at the end.)

**Expected AI behavior:**

1. AI greets the caller and gathers the issue and address naturally.
2. AI captures the name "Jia En Tan" for records — does NOT say "Thanks, Jia En" or "Okay, Jia En Tan" anywhere before the booking readback.
3. If additional address details are needed, AI asks a single natural open question ("What's the address where you need the service?") rather than a three-part walkthrough.
4. Immediately before calling `book_appointment`, AI reads back the name and full service address in one utterance (e.g., "Just to confirm — I have Jia En Tan at 42 Lornie Road, Singapore 298107. Does that sound right?").
5. `book_appointment` fires right after the caller confirms the readback.

**Pass criteria:**

- Zero vocative name use (no "Thanks, Jia En", "Okay Jia En Tan", etc.) at any point before the booking readback.
- The readback is a single utterance containing both name and address.
- `book_appointment` fires immediately after the readback is confirmed.
- No three-part address walkthrough (street → postal → unit) prompting.

---

## Persona 2 — Casual one-breath address (SG-style lead)

**Addresses D-IDs:** D-06, D-07
**Language:** en

**Script (what the tester says):**

1. "Yeah it's Jurong West, block 6, unit 12-345."
2. (Wait for the AI's follow-up question.)
3. Respond to the one follow-up with the missing piece (e.g., postal code: "640006").
4. (Continue the call normally.)

**Expected AI behavior:**

1. AI extracts the area (Jurong West), block (6), and unit (12-345) from the single utterance.
2. AI identifies the one missing piece needed to locate the address (e.g., postal code) and asks exactly one targeted follow-up question for that specific missing piece.
3. AI does NOT launch a mechanical walkthrough asking for street name, then postal code, then unit separately.
4. After the follow-up is answered, AI proceeds with the captured complete address.

**Pass criteria:**

- The AI follow-up is a single targeted question, not a list of address fields.
- AI does not re-ask for information the caller already provided (Jurong West, block 6, unit 12-345).
- No three-part enumeration of address fields.

---

## Persona 3 — Mid-readback correction (US-style lead)

**Addresses D-IDs:** D-05, D-09, D-10
**Language:** en

**Script (what the tester says):**

1. "My name is Sam Johnson. I need a plumber at 123 Main Street, Austin, Texas 78701."
2. (Follow along normally until the booking readback.)
3. When AI reads back details, correct the street number: "No, it's 125, not 123."
4. When AI re-reads the corrected full address, correct the name: "And it's spelled with an h — Johnson with an h at the end. Wait, actually Sam Johnston with an e."
5. Confirm the second re-read: "Yes, that's right."

**Expected AI behavior:**

1. AI accepts the first correction ("125, not 123") and immediately re-reads the corrected full name + address: "Sam Johnson at 125 Main Street, Austin, Texas 78701."
2. AI accepts the second correction ("Johnston with an e") and re-reads again: "Sam Johnston at 125 Main Street, Austin, Texas 78701."
3. AI calls `book_appointment` only after the caller confirms the second re-read.
4. AI does NOT stop re-reading after the first correction — the loop continues until the caller stops correcting.

**Pass criteria:**

- At least two readback iterations occurred.
- Each re-read contained the full corrected name + address (not just the corrected piece).
- `book_appointment` fired only after the caller stopped correcting.
- AI did not revert to the earlier incorrect version at any point.

---

## Persona 4 — Caller invites name use (D-03 override)

**Addresses D-IDs:** D-03
**Language:** en

**Script (what the tester says):**

1. (Start the call and give a name, issue, and address normally.)
2. Early in the call, say: "You can call me Sam."
3. (Continue the call naturally.)
4. (Accept the booking at the end.)

**Expected AI behavior:**

1. Before the caller says "you can call me Sam", AI does not use the name vocatively.
2. After the invitation, AI may naturally use the name "Sam" during the call — it is not required on every turn, but at least once is expected.
3. The booking readback still fires before `book_appointment` (per D-02).
4. AI does not over-use the name — it is natural, not mechanical.

**Pass criteria:**

- No vocative name use before the caller's explicit invitation.
- At least one natural use of "Sam" after the invitation.
- The booking readback still occurs before `book_appointment` fires.
- Subjective feel: natural, not scripted or repetitive.

---

## Persona 5 — Caller refuses name (D-04)

**Addresses D-IDs:** D-04
**Language:** en

**Script (what the tester says):**

1. (Start the call and give an issue and address.)
2. When AI asks for a name: "I'd rather not give a name."
3. (Continue the call — book the appointment without providing a name.)

**Expected AI behavior:**

1. AI does NOT insist on a name or loop asking again.
2. AI proceeds with the call using just the address and issue.
3. Before calling `book_appointment`, the readback contains only the service address (no name portion, no pause to ask for name again).
4. `book_appointment` fires and the booking completes.
5. The Supabase `appointments` row has `caller_name` as null or empty.

**Pass criteria:**

- Booking completes without a name being provided.
- The readback contains only the address (no "I have [blank] at...").
- AI does not pause to ask for the name again during the readback.
- Supabase `appointments` row reflects empty or null `caller_name`.

---

## Persona 6 — Caller declines to book (D-11, D-12 decline path)

**Addresses D-IDs:** D-11, D-12
**Language:** en

**Script (what the tester says):**

1. "Hi, my name is Alex Rivera. I have a leaky faucet at 456 Oak Street, Unit 3, Chicago, IL 60601."
2. After address intake, say: "Actually, I just want someone to call me back later — I'm not ready to book right now."
3. (Continue until AI captures the lead.)

**Expected AI behavior:**

1. AI does the single-question address intake (same as booking path — D-06/D-07/D-08 rules apply).
2. Before calling `capture_lead`, AI reads back the name and full address once (same readback-before-tool rule as D-02 on the booking path).
3. `capture_lead` fires once after the readback is confirmed.
4. AI does NOT re-use the name vocatively after the decline (no "Okay, Alex...").

**Pass criteria:**

- Readback occurred on the decline path before `capture_lead` fired.
- `capture_lead` fired exactly once.
- The single-question address opener was used (not a three-part walkthrough).
- No vocative name use after the decline decision.

---

## Persona 7 — Spanish caller

**Addresses D-IDs:** D-13
**Language:** es

**Script (what the tester says):**

1. Speak in Spanish: "Hola, me llamo María García. Necesito un plomero en Calle Mayor 45, Madrid 28013."
2. (Follow along in Spanish — answer any questions.)
3. (Accept the booking at the end in Spanish.)

**Expected AI behavior:**

1. AI switches to Spanish on the caller's first Spanish utterance (or explicit request).
2. AI uses a single natural Spanish question for address intake (e.g., "¿Cuál es la dirección donde necesita el servicio?") — not a three-part walkthrough.
3. AI captures the name "María García" silently — does NOT say "Gracias, María" or "Muy bien, María García" anywhere before the booking readback.
4. Before `book_appointment`, AI reads back name and address once in Spanish (e.g., "Perfecto — tengo a María García en Calle Mayor 45, Madrid 28013. ¿Es correcto?").
5. All D-01 through D-12 rules apply — same structure as English, localized phrasing.

**Pass criteria:**

- No English drift after the language switch.
- The readback is in Spanish and contains both name and address.
- No vocative name use before the readback.
- Single-question address opener in Spanish (not three-part walkthrough).
- `book_appointment` fires after the Spanish readback is confirmed.
- Same rule count as the English personas (D-01..D-12 all honored).
