"""FastAPI app for the Voco webhook service.

Replaces the stdlib HTTPServer in the deleted src/health.py. Owns port 8080
exclusively. Exposes:

    GET /health      - liveness probe (ported from src/health.py)
    GET /health/db   - DB connectivity probe (ported from src/health.py)
    POST /twilio/*   - four signature-gated Twilio webhook endpoints
                       (mounted via include_router from twilio_routes.py)

Boot: started from src/agent.py __main__ via start_webhook_server() in a
daemon thread before cli.run_app(). See src/webhook/__init__.py.

Phase 39 scope anchor: /twilio/incoming-call always returns a hardcoded AI
TwiML branch. No real schedule evaluation, no pickup number routing, no
subscription gate. Phase 40 wires those in. See D-13 for rationale.
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .twilio_routes import router as twilio_router

logger = logging.getLogger("voco-webhook")

_VERSION = "1.0.0"
_start_time = time.time()

app = FastAPI(
    title="Voco Webhook",
    description="Twilio webhook + health endpoints for the LiveKit voice agent",
    version=_VERSION,
)


@app.on_event("startup")
async def _on_startup() -> None:
    logger.info(f"[webhook] FastAPI app started, version={_VERSION}")


@app.get("/health")
async def health() -> JSONResponse:
    """Liveness probe — always returns 200 while the process is alive.

    Dockerfile HEALTHCHECK relies on this endpoint at http://localhost:8080/health.
    """
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "uptime": round(time.time() - _start_time),
            "version": _VERSION,
        },
    )


@app.get("/health/db")
async def health_db() -> JSONResponse:
    """DB connectivity probe — SELECT 1 against tenants.

    Ported from src/health.py _handle_db_check. Wrapped in asyncio.to_thread
    so the sync supabase-py call doesn't block the ASGI event loop.
    """
    try:
        from src.supabase_client import get_supabase_admin

        def _query():
            supabase = get_supabase_admin()
            return supabase.table("tenants").select("id").limit(1).execute()

        await asyncio.to_thread(_query)
        return JSONResponse(
            status_code=200,
            content={"status": "ok", "db": "connected"},
        )
    except Exception as e:
        logger.error(f"[webhook] /health/db failed: {e}")
        return JSONResponse(
            status_code=503,
            content={"status": "error", "message": str(e)},
        )


# Mount Twilio routes at /twilio/* with router-level signature verification
app.include_router(twilio_router)
