"""Thin wrapper over Garmin's official Personal API client."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from garmin_personal_api import GarminPersonalApiClient, HttpError, PersonalApiAuthenticator

log = logging.getLogger(__name__)


class GarminAuthError(Exception):
    """The Personal API key was rejected (expired or revoked)."""


@dataclass
class GarminActivity:
    id: int
    name: str | None
    type_key: str | None
    start_gmt: str | None
    raw: dict[str, Any]


class Garmin:
    def __init__(self, api_key: str, token_cache: Path):
        self._api_key = api_key
        self._token_cache = token_cache

    def _client(self, *, force_refresh: bool = False) -> GarminPersonalApiClient:
        try:
            with PersonalApiAuthenticator(bearer_token_cache_file=self._token_cache) as auth:
                token = auth.obtain_token(self._api_key, force_refresh=force_refresh)
        except HttpError as exc:
            if exc.status_code in (401, 403):
                raise GarminAuthError(
                    "Garmin rejected the Personal API key. It may have expired; create a new one at "
                    "https://connect.garmin.com/app/settings/personalApiToken"
                ) from exc
            raise
        return GarminPersonalApiClient(token.access_token)

    def _call(self, fn):
        """Run fn(client), retrying once with a fresh token on 401."""
        with self._client() as client:
            try:
                return fn(client)
            except HttpError as exc:
                if exc.status_code != 401:
                    raise
        log.info("Garmin token rejected; refreshing")
        with self._client(force_refresh=True) as client:
            return fn(client)

    def recent_activities(self, limit: int = 20) -> list[GarminActivity]:
        # Garmin returns HTTP 500 for pages larger than ~45.
        data = self._call(lambda c: c.search_activities(start=0, limit=min(limit, 40)))
        items = data if isinstance(data, list) else (data or {}).get("activityList", [])
        return [_parse(a) for a in items if a.get("activityId")]

    def download(self, activity_id: int) -> bytes:
        return self._call(lambda c: c.download_activity(activity_id))


def _parse(a: dict[str, Any]) -> GarminActivity:
    type_info = a.get("activityType") or {}
    return GarminActivity(
        id=int(a["activityId"]),
        name=a.get("activityName"),
        type_key=type_info.get("typeKey") if isinstance(type_info, dict) else None,
        start_gmt=_as_str(a.get("startTimeGMT")),
        raw=a,
    )


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)
