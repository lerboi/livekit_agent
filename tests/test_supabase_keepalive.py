"""P1.10 (2026-09-04): Supabase keep-alive client + prewarm ping.

supabase 2.28.3 honours SyncClientOptions(httpx_client=...) for the
postgrest / storage / auth / functions sub-clients (supabase/_sync/client.py);
postgrest passes full URLs + explicit headers per request, so the supplied
client needs neither base_url nor headers. These tests pin the client's
transport settings, the env rollback, the fail-open construction path, and
the never-raise contract of the prewarm ping.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from src import supabase_client as sc


@pytest.fixture(autouse=True)
def _fresh_env(monkeypatch):
    monkeypatch.setenv("NEXT_PUBLIC_SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-test-key")
    monkeypatch.setattr(sc, "_supabase", None)
    yield
    monkeypatch.setattr(sc, "_supabase", None)


def test_keepalive_client_transport_settings(monkeypatch):
    monkeypatch.setattr(sc, "SUPABASE_KEEPALIVE", True)
    captured = {}

    def _fake_create_client(url, key, options=None):
        captured["url"] = url
        captured["options"] = options
        return MagicMock(name="supabase-client")

    with patch.object(sc, "create_client", _fake_create_client):
        client = sc.get_supabase_admin()
        # Lazily created module global — same object on the second call.
        assert sc.get_supabase_admin() is client

    http = captured["options"].httpx_client
    assert isinstance(http, httpx.Client)
    # Never expire idle keep-alive connections (httpx default is 5 s).
    pool = http._transport._pool
    assert pool._keepalive_expiry is None
    assert pool._max_keepalive_connections == 10
    assert http.timeout == httpx.Timeout(sc.SUPABASE_REQUEST_TIMEOUT_S)
    assert sc.SUPABASE_REQUEST_TIMEOUT_S == 10.0
    assert captured["url"] == "https://example.supabase.co"


def test_keepalive_client_uses_http2():
    http = httpx.Client(
        http2=True,
        timeout=httpx.Timeout(10.0),
        limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=None),
    )
    assert http._transport._pool._http2 is True
    http.close()


def test_flag_off_uses_plain_create_client(monkeypatch):
    monkeypatch.setattr(sc, "SUPABASE_KEEPALIVE", False)
    with patch.object(sc, "create_client") as create:
        sc.get_supabase_admin()
    create.assert_called_once_with("https://example.supabase.co", "service-role-test-key")


def test_keepalive_construction_failure_falls_back_to_plain_client(monkeypatch):
    monkeypatch.setattr(sc, "SUPABASE_KEEPALIVE", True)
    fallback = MagicMock(name="plain-client")
    with patch.object(sc, "_build_keepalive_client", side_effect=RuntimeError("no h2")), \
         patch.object(sc, "create_client", return_value=fallback) as create:
        assert sc.get_supabase_admin() is fallback
    create.assert_called_once_with("https://example.supabase.co", "service-role-test-key")


def test_missing_env_still_raises(monkeypatch):
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY")
    with pytest.raises(RuntimeError):
        sc.get_supabase_admin()


def test_warm_supabase_connection_pings_tenants():
    client = MagicMock()
    with patch.object(sc, "get_supabase_admin", return_value=client):
        assert sc.warm_supabase_connection() is True
    client.table.assert_called_once_with("tenants")
    client.table.return_value.select.assert_called_once_with("id")
    client.table.return_value.select.return_value.limit.assert_called_once_with(1)
    client.table.return_value.select.return_value.limit.return_value.execute.assert_called_once()


def test_warm_supabase_connection_never_raises():
    with patch.object(sc, "get_supabase_admin", side_effect=RuntimeError("env missing")):
        assert sc.warm_supabase_connection() is False
    client = MagicMock()
    client.table.return_value.select.return_value.limit.return_value.execute.side_effect = (
        httpx.ConnectError("down")
    )
    with patch.object(sc, "get_supabase_admin", return_value=client):
        assert sc.warm_supabase_connection() is False


def test_prewarm_calls_supabase_ping_after_vad(monkeypatch):
    from src import agent
    monkeypatch.setattr(agent, "SUPABASE_PREWARM", True)
    proc = MagicMock()
    proc.userdata = {}
    with patch.object(agent.silero.VAD, "load", return_value="vad-obj"), \
         patch.object(agent, "warm_supabase_connection", return_value=True) as ping:
        agent.prewarm(proc)
    assert proc.userdata["vad"] == "vad-obj"
    ping.assert_called_once()


def test_prewarm_skips_ping_when_flag_off(monkeypatch):
    from src import agent
    monkeypatch.setattr(agent, "SUPABASE_PREWARM", False)
    proc = MagicMock()
    proc.userdata = {}
    with patch.object(agent.silero.VAD, "load", return_value="vad-obj"), \
         patch.object(agent, "warm_supabase_connection") as ping:
        agent.prewarm(proc)
    ping.assert_not_called()


def test_worker_pool_pinned_via_env(monkeypatch):
    from src import agent
    assert agent.NUM_IDLE_PROCESSES == 2  # default when VOCO_NUM_IDLE_PROCESSES unset
