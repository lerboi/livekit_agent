"""Outlook Calendar connection (Microsoft Graph): create, move, delete one event.

Mirrors ``calendars.google.GoogleCalendar``. Synchronous; call through
``asyncio.to_thread``. Not yet exercised against a live Outlook account.
"""

from __future__ import annotations

import logging
import os
import time

import requests

from .google import CalendarError, to_naive_local_iso

logger = logging.getLogger("voco-agent")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
HTTP_TIMEOUT_S = 8
EXPIRY_MARGIN_MS = 300_000


class OutlookCalendar:
    provider = "outlook"

    def __init__(self, tenant_id: str, creds: dict, supabase):
        self.tenant_id = tenant_id
        self._creds = dict(creds)
        self._supabase = supabase

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
            raise CalendarError("outlook: no refresh token stored")
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": os.environ.get("MICROSOFT_CLIENT_ID", ""),
                "client_secret": os.environ.get("MICROSOFT_CLIENT_SECRET", ""),
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "scope": "https://graph.microsoft.com/.default offline_access",
            },
            timeout=HTTP_TIMEOUT_S,
        )
        if resp.status_code != 200:
            raise CalendarError(f"outlook: token refresh failed ({resp.status_code})")
        tokens = resp.json()
        update = {
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token", refresh_token),
            "expiry_date": int(time.time() * 1000) + int(tokens.get("expires_in", 3600)) * 1000,
        }
        self._creds.update(update)
        try:
            (
                self._supabase.table("calendar_credentials")
                .update(update)
                .eq("tenant_id", self.tenant_id)
                .eq("provider", "outlook")
                .execute()
            )
        except Exception as err:  # noqa: BLE001
            logger.warning("[calendar] could not persist refreshed Outlook token: %s", err)
        return update["access_token"]

    def prepare(self) -> None:
        if not self._token_is_fresh():
            self._refresh()

    def _request(self, method: str, path: str, **kwargs):
        token = self._creds.get("access_token") if self._token_is_fresh() else self._refresh()
        url = f"{GRAPH_BASE}/me/events{path}"
        resp = requests.request(
            method, url, headers={"Authorization": f"Bearer {token}"},
            timeout=HTTP_TIMEOUT_S, **kwargs,
        )
        if resp.status_code == 401:
            token = self._refresh()
            resp = requests.request(
                method, url, headers={"Authorization": f"Bearer {token}"},
                timeout=HTTP_TIMEOUT_S, **kwargs,
            )
        return resp

    @staticmethod
    def _times(start_utc: str, end_utc: str, tz: str) -> dict:
        return {
            "start": {"dateTime": to_naive_local_iso(start_utc, tz), "timeZone": tz},
            "end": {"dateTime": to_naive_local_iso(end_utc, tz), "timeZone": tz},
        }

    def create_event(self, *, title: str, body: str, start_utc: str, end_utc: str, tz: str) -> dict:
        resp = self._request(
            "POST", "",
            json={
                "subject": title,
                "body": {"contentType": "text", "content": body},
                **self._times(start_utc, end_utc, tz),
            },
        )
        if resp.status_code not in (200, 201):
            raise CalendarError(f"outlook: create failed ({resp.status_code})")
        event = resp.json()
        return {"provider": "outlook", "event_id": event.get("id"), "html_link": event.get("webLink")}

    def move_event(self, event_id: str, *, start_utc: str, end_utc: str, tz: str) -> dict:
        resp = self._request("PATCH", f"/{event_id}", json=self._times(start_utc, end_utc, tz))
        if resp.status_code != 200:
            raise CalendarError(f"outlook: move failed ({resp.status_code})")
        event = resp.json()
        return {"provider": "outlook", "event_id": event.get("id"), "html_link": event.get("webLink")}

    def delete_event(self, event_id: str) -> None:
        resp = self._request("DELETE", f"/{event_id}")
        if resp.status_code not in (200, 204, 404, 410):
            raise CalendarError(f"outlook: delete failed ({resp.status_code})")
