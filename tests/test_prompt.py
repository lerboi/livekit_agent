"""Prompt contract tests for the 2026-09-09 rewrite (call-experience item 5).

History: Phase 60.2 reverted a runtime filler (session.say on a
RealtimeModel-only session could not produce audio) and made the PROMPT own
the filler ("never emit a tool call without speaking first"). Phase 60.3
Branch P then made the goodbye a two-turn move (speak, wait, end_call in a
separate turn) because end_call fired mid-farewell under the realtime model.

Both are inverted here, on purpose and on a verified SDK basis (livekit-agents
1.8, cascaded pipeline with a real TTS):
- Latency cover is RUNTIME-owned again — `RunContext.with_filler()` speaks a
  locale-correct line only when a tool is actually slow, and a typing sound
  plays while a tool runs (src/lib/tool_filler.py). So the prompt must NOT
  tell the model to speak a filler first (that would double-cover every tool
  and cost a second LLM round-trip), and the tool descriptions must tell the
  model to call the tool directly.
- end_call awaits `RunContext.wait_for_playout()` — the goodbye spoken in the
  same turn — before disconnecting, and returns None so no follow-up reply
  can be spoken over the hang-up. So the prompt must teach goodbye + end_call
  in the SAME turn.

Invariants asserted here:
1. No TOOL NARRATION section; no "speak first" instruction anywhere in the
   assembled prompt; no literal filler bank for the model to recite.
2. Tool descriptions for the availability/booking/address tools say to call
   the tool directly without announcing it.
3. ENDING THE CALL is a CRITICAL RULE block teaching the same-turn goodbye
   with WRONG/RIGHT framing, keeps the 9/10-minute bounds, sits in the
   top attention band (before OPENING), and never claims session.say.
"""
from __future__ import annotations

import json

from src.prompt import _build_call_duration_section, build_system_prompt


def _assembled(**overrides) -> str:
    kwargs = dict(locale="en", business_name="Voco", onboarding_complete=True)
    kwargs.update(overrides)
    return build_system_prompt(**kwargs)


# ── 1. Runtime-owned filler: the prompt no longer asks the model to speak first ──


def test_no_tool_narration_section_in_assembled_prompt():
    p = _assembled()
    assert "TOOL NARRATION" not in p
    lowered = p.lower()
    assert "never emit a tool call without speaking first" not in lowered
    assert "speak one short, varied filler" not in lowered
    assert "speak the filler" not in lowered
    # No recitable filler bank left in the prompt (the runtime owns the lines).
    for phrase in ("let me pull that up", "hang on, checking that slot", "locking that in for you now"):
        assert phrase not in lowered, phrase


def test_prompt_tells_model_tools_are_called_directly():
    p = _assembled().lower()
    # The scheduling rule and the address rule both say the wait is covered.
    assert "without announcing it" in p
    assert "the system covers the wait" in p


def test_tool_descriptions_say_call_directly_without_announcing():
    from src.tools.check_slot import _SCHEMA as check_slot_schema
    from src.tools.check_day import _SCHEMA as check_day_schema
    from src.tools.next_available_days import _SCHEMA as nad_schema
    from src.tools.validate_address import _SCHEMA as validate_schema
    from src.tools.book_appointment import _BOOK_APPOINTMENT_SCHEMA as book_schema

    for schema in (check_slot_schema, check_day_schema, nad_schema, validate_schema, book_schema):
        desc = schema["description"]
        assert "without announcing it" in desc, schema["name"]
        assert "TOOL NARRATION" not in desc, schema["name"]
        assert "filler" not in desc.lower(), schema["name"]
        # STATE+DIRECTIVE contract unchanged: never read the return aloud.
        assert "state+directive" in desc.lower(), schema["name"]


def test_runtime_filler_banks_exist_in_both_locales_and_never_name_a_time():
    """The lines the runtime speaks live in the message bundles. They must
    exist for EN and ES with the same bank names, and must never contain a
    clock time / date word — the prompt's anti-fabrication rule forbids a
    filler that primes 'four PM is available'."""
    import pathlib

    root = pathlib.Path(__file__).parent.parent / "src" / "messages"
    en = json.loads((root / "en.json").read_text(encoding="utf-8"))["tool_fillers"]
    es = json.loads((root / "es.json").read_text(encoding="utf-8"))["tool_fillers"]
    assert set(en) == set(es)
    assert {"generic", "address", "booking", "lead", "still_working"} <= set(en)
    banned = ("am", "pm", "o'clock", "monday", "tuesday", "wednesday", "thursday", "friday",
              "saturday", "sunday", "tomorrow", "today", "lunes", "martes", "mañana", "hoy")
    for bundle in (en, es):
        for name, lines in bundle.items():
            assert isinstance(lines, list) and lines, name
            assert len(set(lines)) == len(lines), f"duplicate filler in {name}"
            for line in lines:
                words = {w.strip(".,!?").lower() for w in line.split()}
                assert not (words & set(banned)), (name, line)
                assert not any(ch.isdigit() for ch in line), (name, line)


# ── 3. Same-turn goodbye ───────────────────────────────────────────────────────


def _t_stub(key: str) -> str:
    return key


def test_call_duration_is_critical_rule_framed():
    section = _build_call_duration_section(_t_stub)
    assert "ENDING THE CALL — CRITICAL RULE:" in section


def test_call_duration_teaches_same_turn_goodbye_with_failure_mode():
    section = _build_call_duration_section(_t_stub)
    lowered = section.lower()
    assert "same turn" in lowered
    assert "end_call" in section
    # The runtime guarantee the rule relies on is stated to the model.
    assert "finished playing" in lowered
    # WRONG / RIGHT framing retained.
    assert "WRONG" in section or "Failure mode" in section
    assert "RIGHT" in section or "Correct path" in section
    # The old two-turn choreography is gone.
    assert "separate turn" not in lowered
    assert "let a brief silence pass" not in lowered


def test_call_duration_preserves_9_and_10_minute_bounds():
    section = _build_call_duration_section(_t_stub)
    assert "9 minutes" in section
    assert "10 minutes" in section


def test_call_duration_in_top_attention_band():
    assembled = _assembled()
    assert assembled.index("ENDING THE CALL — CRITICAL RULE") < assembled.index("OPENING:")
    assert assembled.index("ENDING THE CALL — CRITICAL RULE") < assembled.index("BOOKING:")


def test_call_duration_does_not_claim_session_say():
    section = _build_call_duration_section(_t_stub).lower()
    assert "session.say" not in section
    assert "runtime plays" not in section


def test_final_recap_matches_same_turn_goodbye():
    assembled = _assembled()
    final = assembled[assembled.index("FINAL — NON-NEGOTIABLES"):]
    lowered = final.lower()
    assert "end_call" in final
    assert "same turn" in lowered
    assert "separate turn" not in lowered
