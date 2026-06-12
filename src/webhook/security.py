"""Twilio signature verification FastAPI dependency.

Applied at the router level on /twilio/* so all four endpoints are gated
without per-route boilerplate. Raises HTTPException(403) on invalid or
missing signature.

Env var ALLOW_UNSIGNED_WEBHOOKS=true bypasses verification with a warning
log — dev and staging only. Fail-closed default: if the env var is unset,
validation always runs.

URL reconstruction trusts x-forwarded-proto and host headers, matching
Railway's edge proxy configuration (RESEARCH.md §3 "URL Reconstruction
Behind Railway's Proxy"). uvicorn must be started with proxy_headers=True
and forwarded_allow_ips='*' for this to work — handled in
src/webhook/__init__.py.
"""
from __future__ import annotations

import logging
import os

from fastapi import HTTPException, Request
from twilio.request_validator import RequestValidator

logger = logging.getLogger("voco-webhook")


async def verify_twilio_signature(request: Request) -> None:
    """FastAPI dependency. Raises 403 if Twilio signature is invalid.

    Also reads the form body once and stashes it on request.state.form_data
    so route handlers can access it without re-parsing (Starlette caches
    internally but this is explicit and version-independent).
    """
    if os.environ.get("ALLOW_UNSIGNED_WEBHOOKS", "").lower() == "true":
        # Dev convenience only. NEVER honored in production — one stray env
        # var would otherwise disable signature checks entirely
        # (2026-06-12 audit M9). Unset PYTHON_ENV defaults to "production"
        # (fail closed), matching the Sentry init default in agent.py.
        if os.environ.get("PYTHON_ENV", "production").lower() == "production":
            logger.error(
                "[webhook] ALLOW_UNSIGNED_WEBHOOKS=true ignored: PYTHON_ENV is "
                "production — signature check enforced"
            )
        else:
            logger.warning(
                "[webhook] ALLOW_UNSIGNED_WEBHOOKS=true — skipping signature check"
            )
            # Still read form so handlers can access via request.state uniformly
            form_data = await request.form()
            request.state.form_data = dict(form_data)
            return

    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    signature = request.headers.get("X-Twilio-Signature", "")

    # Fail closed on a missing/blank auth token. RequestValidator("") computes
    # HMACs with an EMPTY key, so anyone could forge valid signatures — a
    # misconfigured deploy must reject webhooks, not silently accept forgeries
    # (2026-06-12 audit M9).
    if not auth_token:
        logger.error("[webhook] TWILIO_AUTH_TOKEN is not set — rejecting webhook")
        raise HTTPException(status_code=503, detail="Webhook validation unavailable")

    # Reconstruct URL the way Twilio signed it (D-15)
    proto = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("host", "")
    url = f"{proto}://{host}{request.url.path}"

    form_data = await request.form()
    params = dict(form_data)
    request.state.form_data = params

    validator = RequestValidator(auth_token)
    if not validator.validate(url, params, signature):
        logger.warning(f"[webhook] Signature validation failed: url={url}")
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
