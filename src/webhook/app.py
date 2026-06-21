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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from .twilio_routes import router as twilio_router, _ai_sip_twiml, _empty_twiml
from src.lib.phone import _normalize_phone

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


# LK-B4: the Twilio voice front door must NEVER return a non-TwiML 5xx — Twilio
# would play its generic "application error, goodbye" and hang up the call. This
# scoped global handler is the second line of defense behind the per-step
# try/excepts in twilio_routes: ANY otherwise-unhandled error on a voice route
# falls open to AI SIP TwiML (the caller still reaches the AI receptionist).
_VOICE_FAILOPEN_PATHS = {
    "/twilio/incoming-call",
    "/twilio/dial-status",
    "/twilio/dial-fallback",
}


@app.exception_handler(Exception)
async def _failopen_exception_handler(request: Request, exc: Exception) -> Response:
    # NEVER mask an HTTP error (e.g. the fail-CLOSED Twilio signature 401/403) as a
    # 200 — only genuine unhandled 500-class errors fall open below.
    if isinstance(exc, (HTTPException, StarletteHTTPException)):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    path = request.url.path
    logger.error("[webhook] Unhandled error on %s: %s", path, exc, exc_info=True)

    if path in _VOICE_FAILOPEN_PATHS:
        # LK-B3: even on the catastrophic-error fallback, template the dialed
        # number when the signature dep already stashed the form (it runs before
        # the route). Best-effort — falls back to the static URI on any issue.
        to_number = None
        try:
            form_data = getattr(request.state, "form_data", None) or {}
            to_number = _normalize_phone(form_data.get("To", "") or "") or None
        except Exception:
            to_number = None
        return Response(content=_ai_sip_twiml(to_number), media_type="application/xml")
    if path == "/twilio/incoming-sms":
        # SMS webhook: an empty messaging response is the safe no-op (no auto-reply).
        return Response(content=_empty_twiml(), media_type="application/xml")
    # Non-Twilio routes (health, unknown): standard 500.
    return JSONResponse(
        status_code=500, content={"status": "error", "message": "internal error"}
    )


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
