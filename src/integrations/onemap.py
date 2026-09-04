"""
OneMap (Singapore Land Authority) postal-code lookup — P1.4, 2026-09-04.

Why: a Singapore postal code identifies a single building. Deepgram
transcribes digit strings reliably and proper-noun street names unreliably
("Burr Drive" / "Kenboro Drive" for Canberra Drive on the 2026-08-17 call),
so for SG tenants the postal code — not the street — is the thing to resolve.
OneMap's search endpoint is free, needs no auth, and returns the block
number, road name and building for a 6-digit postal code.

Contract (mirrors google_maps.py):
- per-call `httpx.AsyncClient`, hard timeout, NEVER raises — every failure
  (timeout, HTTP 5xx, bad JSON, no results) returns None and logs a warning.
- Only called for a 6-digit postal code; anything else short-circuits to None
  without a network round-trip.

Verified live 2026-09-01:
  GET https://www.onemap.gov.sg/api/common/elastic/search?searchVal=768433
      &returnGeom=Y&getAddrDetails=Y&pageNum=1
  → {"found":1,"results":[{"SEARCHVAL":"YISHUN SAPPHIRE","BLK_NO":"40",
     "ROAD_NAME":"CANBERRA DRIVE","BUILDING":"YISHUN SAPPHIRE",
     "ADDRESS":"40 CANBERRA DRIVE YISHUN SAPPHIRE SINGAPORE 768433",
     "POSTAL":"768433", ...}]}
  BUILDING is the literal string "NIL" for landed / private houses.
"""

from __future__ import annotations

import logging
import re

import httpx

logger = logging.getLogger(__name__)

ONEMAP_SEARCH_URL = "https://www.onemap.gov.sg/api/common/elastic/search"

_POSTAL_RE = re.compile(r"\d{6}")


def is_sg_postal(postal: str | None) -> bool:
    """True for exactly six digits (after dropping spaces/hyphens the LLM may
    have inserted when converting spoken digits)."""
    return bool(_POSTAL_RE.fullmatch(normalize_postal(postal)))


def normalize_postal(postal: str | None) -> str:
    return re.sub(r"[\s-]", "", postal or "")


async def lookup_postal(postal: str, *, timeout_seconds: float = 1.5) -> dict | None:
    """Resolve a Singapore postal code to its first OneMap result dict
    (keys: BLK_NO, ROAD_NAME, BUILDING, ADDRESS, POSTAL, optionally
    LATITUDE/LONGITUDE). Returns None on no hit or any failure. Never raises."""
    postal = normalize_postal(postal)
    if not _POSTAL_RE.fullmatch(postal):
        return None

    params = {
        "searchVal": postal,
        "returnGeom": "Y",
        "getAddrDetails": "Y",
        "pageNum": "1",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds)) as client:
            resp = await client.get(ONEMAP_SEARCH_URL, params=params)
            if resp.status_code != 200:
                logger.warning(
                    "[onemap] postal=%s HTTP %s", postal, resp.status_code
                )
                return None
            data = resp.json()
    except Exception as exc:  # noqa: BLE001 — never raises (timeout, transport, JSON)
        logger.warning("[onemap] postal=%s lookup failed: %s", postal, exc)
        return None

    results = (data or {}).get("results") if isinstance(data, dict) else None
    if not results or not isinstance(results, list):
        return None
    first = results[0]
    if not isinstance(first, dict):
        return None
    # A hit must carry the postal we asked for — OneMap's fuzzy search can
    # return unrelated matches for malformed input.
    if str(first.get("POSTAL") or "") != postal:
        logger.warning(
            "[onemap] postal=%s first result carries POSTAL=%r — ignoring",
            postal, first.get("POSTAL"),
        )
        return None
    return first
