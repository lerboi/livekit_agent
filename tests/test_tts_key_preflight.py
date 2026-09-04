"""2026-09-04: ElevenLabs key preflight (VOCO_TTS_KEY_PREFLIGHT).

A rejected ELEVEN_API_KEY (the key *ID* pasted instead of the `sk_…` secret)
used to cost every call ~5 s of FallbackAdapter retries before OpenAI TTS
spoke the greeting — 6-7 s of dead air, wrong voice, greeting-mute cap firing
mid-sentence, and no visible signal. prewarm() now classifies the key once per
job process and the entrypoint builds OpenAI TTS directly on an explicit
rejection. Network problems / unknown responses must keep ElevenLabs first.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src import agent


def _resp(status: int, text: str = ""):
    return SimpleNamespace(status_code=status, text=text)


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("ELEVEN_API_KEY", "1e1notasecret")


def test_accepted_key_returns_true():
    with patch("httpx.get", return_value=_resp(200, '{"subscription": {}}')):
        assert agent._check_elevenlabs_key() is True


@pytest.mark.parametrize("status,body", [
    (400, '{"detail":{"type":"authentication_error","code":"invalid_api_key","message":"API key ID used as API key"}}'),
    (401, '{"detail":{"type":"authentication_error","code":"unauthorized","message":"Invalid API key"}}'),
    (401, "Unauthorized"),
])
def test_auth_rejection_returns_false(status, body):
    with patch("httpx.get", return_value=_resp(status, body)):
        assert agent._check_elevenlabs_key() is False


@pytest.mark.parametrize("status,body", [
    (400, '{"detail":{"type":"validation_error"}}'),  # not an auth problem
    (429, "rate limited"),
    (500, "boom"),
    (503, ""),
])
def test_non_auth_failures_are_inconclusive(status, body):
    with patch("httpx.get", return_value=_resp(status, body)):
        assert agent._check_elevenlabs_key() is None


def test_network_error_is_inconclusive_and_never_raises():
    with patch("httpx.get", side_effect=httpx.ConnectTimeout("slow")):
        assert agent._check_elevenlabs_key() is None


def test_missing_key_is_inconclusive_without_network(monkeypatch):
    monkeypatch.delenv("ELEVEN_API_KEY")
    with patch("httpx.get") as get:
        assert agent._check_elevenlabs_key() is None
    get.assert_not_called()


def test_request_shape():
    with patch("httpx.get", return_value=_resp(200)) as get:
        agent._check_elevenlabs_key(timeout_s=2.5)
    args, kwargs = get.call_args
    assert args[0] == "https://api.elevenlabs.io/v1/user"
    assert kwargs["headers"] == {"xi-api-key": "1e1notasecret"}
    assert kwargs["timeout"] == 2.5


# -- prewarm wiring --------------------------------------------------------------

def test_prewarm_stores_verdict(monkeypatch):
    monkeypatch.setattr(agent, "TTS_KEY_PREFLIGHT", True)
    monkeypatch.setattr(agent, "SUPABASE_PREWARM", False)
    proc = MagicMock()
    proc.userdata = {}
    with patch.object(agent.silero.VAD, "load", return_value="vad"), \
         patch.object(agent, "_check_elevenlabs_key", return_value=False):
        agent.prewarm(proc)
    assert proc.userdata["eleven_key_ok"] is False


def test_prewarm_skips_preflight_when_flag_off(monkeypatch):
    monkeypatch.setattr(agent, "TTS_KEY_PREFLIGHT", False)
    monkeypatch.setattr(agent, "SUPABASE_PREWARM", False)
    proc = MagicMock()
    proc.userdata = {}
    with patch.object(agent.silero.VAD, "load", return_value="vad"), \
         patch.object(agent, "_check_elevenlabs_key") as chk:
        agent.prewarm(proc)
    chk.assert_not_called()
    assert "eleven_key_ok" not in proc.userdata


# -- _build_tts selection ----------------------------------------------------------

def test_build_tts_rejected_key_uses_openai_only(monkeypatch):
    openai_tts = MagicMock(name="openai-tts")
    with patch.object(agent.openai, "TTS", return_value=openai_tts) as oa, \
         patch.object(agent.elevenlabs, "TTS") as el:
        tts = agent._build_tts("voice-1", eleven_key_ok=False)
    assert tts is openai_tts
    oa.assert_called_once_with(model=agent.OPENAI_TTS_MODEL, voice=agent.OPENAI_TTS_VOICE)
    el.assert_not_called()


@pytest.mark.parametrize("verdict", [True, None])
def test_build_tts_keeps_elevenlabs_first_otherwise(monkeypatch, verdict):
    """Accepted key AND inconclusive preflight both keep today's construction:
    ElevenLabs (with voice settings) wrapped in the FallbackAdapter over OpenAI."""
    eleven = MagicMock(name="eleven-tts")
    adapter = MagicMock(name="fallback-adapter")
    from livekit.agents import tts as agents_tts
    with patch.object(agent.elevenlabs, "TTS", return_value=eleven) as el, \
         patch.object(agent.openai, "TTS", return_value=MagicMock(name="openai-tts")), \
         patch.object(agents_tts, "FallbackAdapter", return_value=adapter) as fa:
        tts = agent._build_tts("voice-1", eleven_key_ok=verdict)
    assert tts is adapter
    assert el.call_args.kwargs["voice_id"] == "voice-1"
    assert el.call_args.kwargs["model"] == agent.ELEVENLABS_TTS_MODEL
    assert fa.call_args.args[0][0] is eleven
