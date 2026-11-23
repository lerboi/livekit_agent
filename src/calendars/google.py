"""Google Calendar connection: create, move and delete one event.

Talks to the Calendar REST API directly with the tenant's stored OAuth tokens
(``calendar_credentials``). No discovery document and no per-call database
reads: the credential row is loaded once when the connection is opened, so an
event write is a single HTTPS request.

All methods are synchronous; call them through ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger("voco-agent")

API_BASE = "https://www.googleapis.com/calendar/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
HTTP_TIMEOUT_S = 8
# Refresh when the stored token has less than this left.
EXPIRY_MARGIN_MS = 120_000


def to_naive_local_iso(utc_iso: str, tz: str) -> str:
    """'2026-04-30T07:00:00+00:00' + 'Asia/Singapore' -> '2026-04-30T15:00:00'.

    Google and Microsoft both want a naive local dateTime next to an explicit
    timeZone; an offset-suffixed value alongside timeZone is applied twice.
    """
    iso = utc_iso[:-1] + "+00:00" if utc_iso.endswith("Z") else utc_iso
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(tz)).replace(tzinfo=None).isoformat()


class CalendarError(Exception):
    """A calendar write that did not go through."""


class GoogleCalendar:
    provider = "google"

    def __init__(self, tenant_id: str, creds: dict, supabase):
        self.tenant_id = tenant_id
        self._creds = dict(creds)
        self._supabase = supabase
        self.calendar_id = creds.get("calendar_id") or "primary"

    # ── token ──────────────────────────────────────────────────────────────

    def _token_is_fresh(self) -> bool:
        expiry = self._creds.get("expiry_date")
        return bool(
            self._creds.get("access_token")
            and expiry
            and int(expiry) > time.time() * 1000 + EXPIRY_MARGIN_MS
        )

    def _refresh(self) -> str:
        refresh_token = self._creds.get("refresh_token")
        if not refresh_token:
            raise CalendarError("google: no refresh token stored")
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
                "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=HTTP_TIMEOUT_S,
        )
        if resp.status_code != 200:
            raise CalendarError(f"google: token refresh failed ({resp.status_code})")
        tokens = resp.json()
        update = {
            "access_token": tokens["access_token"],
            "expiry_date": int(time.time() * 1000) + int(tokens.get("expires_in", 3600)) * 1000,
        }
        if tokens.get("refresh_token"):
            update["refresh_token"] = tokens["refresh_token"]
        self._creds.update(update)
        try:
            (
                self._supabase.table("calendar_credentials")
                .update(update)
                .eq("tenant_id", self.tenant_id)
                .eq("provider", "google")
                .execute()
            )
        except Exception as err:  # noqa: BLE001 - the token still works for this call
            logger.warning("[calendar] could not persist refreshed Google token: %s", err)
        return update["access_token"]

    def prepare(self) -> None:
        """Make sure a usable access token is in hand (refreshing if needed).

        Called while the greeting plays so the first event write does not pay
        for a token refresh.
        """
        if not self._token_is_fresh():
            self._refresh()

    def _request(self, method: str, path: str, **kwargs):
        token = self._creds.get("access_token") if self._token_is_fresh() else self._refresh()
        url = f"{API_BASE}/calendars/{quote(self.calendar_id, safe='')}/events{path}"
        resp = requests.request(
            method, url, headers={"Authorization": f"Bearer {token}"},
            timeout=HTTP_TIMEOUT_S, **kwargs,
        )
        if resp.status_code == 401:
            # Token revoked or expired early: one forced refresh, one retry.
            token = self._refresh()
            resp = requests.request(
                method, url, headers={"Authorization": f"Bearer {token}"},
                timeout=HTTP_TIMEOUT_S, **kwargs,
            )
        return resp

    # ── events ─────────────────────────────────────────────────────────────

    @staticmethod
    def _times(start_utc: str, end_utc: str, tz: str) -> dict:
        return {
            "start": {"dateTime": to_naive_local_iso(start_utc, tz), "timeZone": tz},
            "end": {"dateTime": to_naive_local_iso(end_utc, tz), "timeZone": tz},
        }

    def create_event(self, *, title: str, body: str, start_utc: str, end_utc: str, tz: str) -> dict:
        resp = self._request(
            "POST", "",
            json={"summary": title, "description": body, **self._times(start_utc, end_utc, tz)},
        )
        if resp.status_code not in (200, 201):
            raise CalendarError(f"google: create failed ({resp.status_code})")
        event = resp.json()
        return {"provider": "google", "event_id": event.get("id"), "html_link": event.get("htmlLink")}

    def move_event(self, event_id: str, *, start_utc: str, end_utc: str, tz: str) -> dict:
        resp = self._request("PATCH", f"/{event_id}", json=self._times(start_utc, end_utc, tz))
        if resp.status_code != 200:
            raise CalendarError(f"google: move failed ({resp.status_code})")
        event = resp.json()
        return {"provider": "google", "event_id": event.get("id"), "html_link": event.get("htmlLink")}

    def delete_event(self, event_id: str) -> None:
        resp = self._request("DELETE", f"/{event_id}")
        # 404/410: already removed by hand in the calendar. That is the goal.
        if resp.status_code not in (200, 204, 404, 410):
            raise CalendarError(f"google: delete failed ({resp.status_code})")
