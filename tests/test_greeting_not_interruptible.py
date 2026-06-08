"""The opening greeting must be NON-INTERRUPTIBLE.

Production symptom (Phase 64 revert, 2026-04-25): the "Thank you for calling ..."
greeting cut itself off halfway — SIP self-echo / line noise tripped the VAD and
cancelled the greeting mid-sentence.

Fix (carried into the Phase 66 cascade pipeline): mute the caller's inbound audio
for the duration of the greeting, then unmute once it has played out. The
greeting is delivered deterministically via `session.say(greeting_text)` (no LLM
turn), and gating the inbound audio is the only reliable way to stop echo from
interrupting it. These are source-grep invariants (same shape as the agent's
other static guards) — behavioral confirmation is the live UAT call.
"""
from pathlib import Path

_AGENT_SRC = Path(__file__).parent.parent / "src" / "agent.py"


def _src() -> str:
    return _AGENT_SRC.read_text(encoding="utf-8")


def test_greeting_mutes_caller_input():
    assert "set_audio_enabled(False)" in _src(), (
        "greeting must mute caller input so VAD/echo cannot interrupt it"
    )


def test_greeting_unmutes_caller_input():
    assert "set_audio_enabled(True)" in _src(), (
        "caller input must be re-enabled after the greeting"
    )


def test_greeting_awaits_playout_with_safety_timeout():
    src = _src()
    assert "wait_for_playout()" in src, "must await the greeting SpeechHandle playout"
    assert "GREETING_UNMUTE_TIMEOUT_S" in src, (
        "must cap the playout wait so input is never left muted on a stalled SIP playout"
    )


def test_greeting_dispatched_via_session_say_non_interruptible():
    # Phase 66: deterministic greeting via session.say (was native generate_reply
    # under the gpt-realtime-2 model). The fixed template means byte-identical
    # wording every call and no LLM turn consumed. allow_interruptions=False is a
    # second echo defense behind the input mute — the cascade AgentSession honors
    # it (the realtime model ignored it).
    assert "session.say(greeting_text, allow_interruptions=False)" in _src()


def test_greeting_timeout_constant_is_reasonable():
    from src.agent import GREETING_UNMUTE_TIMEOUT_S

    # Long enough for a normal greeting (~3-5s), short enough to bound the
    # muted window if playout stalls.
    assert 5.0 <= GREETING_UNMUTE_TIMEOUT_S <= 20.0
