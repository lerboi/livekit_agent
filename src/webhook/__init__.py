"""Voco webhook subpackage — FastAPI app, Twilio routes, schedule evaluator, soft caps.

Boot: call start_webhook_server() from src/agent.py __main__ before cli.run_app().
The webhook runs on port 8080 in a daemon thread that exits with the process.
"""
from __future__ import annotations

import logging
import threading

from .app import app

__all__ = ["app", "start_webhook_server"]


def start_webhook_server() -> None:
    """Start the FastAPI webhook server in a daemon thread.

    Non-blocking: returns immediately after thread.start(). The uvicorn server
    binds port 8080 within ~100ms, which is well before any real Twilio request
    can arrive (LiveKit cli.run_app takes several seconds to connect to the
    LiveKit Cloud).

    Daemon=True: the thread exits automatically when the main process exits
    on Railway SIGTERM. No graceful shutdown is needed because the webhook is
    stateless.

    Replaces src/health.py start_health_server() — FastAPI now owns port 8080
    and serves /health, /health/db, and /twilio/* from the same process.
    """
    import uvicorn  # Lazy import so pytest can import `app` without booting uvicorn

    def _run() -> None:
        try:
            uvicorn.run(
                app,
                host="0.0.0.0",
                port=8080,
                proxy_headers=True,
                forwarded_allow_ips="*",
                log_config=None,
            )
        except Exception as e:
            logging.getLogger("voco-webhook").error(
                f"[webhook] uvicorn failed: {e}"
            )

    thread = threading.Thread(target=_run, daemon=True, name="voco-webhook")
    thread.start()
