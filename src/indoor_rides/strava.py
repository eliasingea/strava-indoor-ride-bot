"""Minimal Strava API client: OAuth token refresh, uploads, activity updates."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)

API = "https://www.strava.com/api/v3"
OAUTH_TOKEN_URL = "https://www.strava.com/oauth/token"
AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
REDIRECT_URI = "http://localhost/exchange_token"
SCOPES = "read,activity:read_all,activity:write"

_DUPLICATE_RE = re.compile(r"duplicate of .*?/activities/(\d+)|duplicate of activity (\d+)", re.I)


class StravaError(Exception):
    pass


class DuplicateActivity(StravaError):
    def __init__(self, message: str, existing_id: int | None):
        super().__init__(message)
        self.existing_id = existing_id


@dataclass
class UploadResult:
    activity_id: int

    @property
    def url(self) -> str:
        return f"https://www.strava.com/activities/{self.activity_id}"


def authorize_url(client_id: str) -> str:
    return f"{AUTHORIZE_URL}?" + urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "approval_prompt": "force",
        "scope": SCOPES,
    })


def exchange_code(client_id: str, client_secret: str, code: str, tokens_path: Path) -> dict:
    resp = requests.post(OAUTH_TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
    }, timeout=30)
    if not resp.ok:
        raise StravaError(f"Token exchange failed ({resp.status_code}): {resp.text}")
    data = resp.json()
    _save_tokens(tokens_path, data)
    return data


class Strava:
    def __init__(self, client_id: str, client_secret: str, tokens_path: Path):
        self._client_id = client_id
        self._client_secret = client_secret
        self._tokens_path = tokens_path
        self._session = requests.Session()

    # auth ---------------------------------------------------------------- #

    def _access_token(self) -> str:
        if not self._tokens_path.exists():
            raise StravaError("Not authorized with Strava yet. Run: indoor-rides strava-auth")
        tokens = json.loads(self._tokens_path.read_text())
        if tokens.get("expires_at", 0) - 300 > time.time():
            return tokens["access_token"]

        resp = self._session.post(OAUTH_TOKEN_URL, data={
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        }, timeout=30)
        if not resp.ok:
            raise StravaError(f"Strava token refresh failed ({resp.status_code}): {resp.text}")
        new = resp.json()
        tokens.update({k: new[k] for k in ("access_token", "refresh_token", "expires_at")})
        _save_tokens(self._tokens_path, tokens)
        return tokens["access_token"]

    def _request(self, method: str, path: str, **kwargs) -> dict:
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        resp = self._session.request(method, f"{API}{path}", headers=headers, timeout=60, **kwargs)
        if resp.status_code == 429:
            raise StravaError("Strava rate limit hit; will retry later")
        if not resp.ok:
            raise StravaError(f"Strava {method} {path} failed ({resp.status_code}): {resp.text}")
        return resp.json()

    # API ----------------------------------------------------------------- #

    def athlete(self) -> dict:
        return self._request("GET", "/athlete")

    def upload(
        self,
        data: bytes,
        *,
        filename: str,
        data_type: str,
        name: str | None = None,
        description: str | None = None,
        trainer: bool = False,
        external_id: str | None = None,
        wait_s: float = 120,
    ) -> UploadResult:
        fields = {"data_type": data_type}
        if name:
            fields["name"] = name
        if description:
            fields["description"] = description
        if trainer:
            fields["trainer"] = "1"
        if external_id:
            fields["external_id"] = external_id

        upload = self._request("POST", "/uploads", data=fields, files={"file": (filename, data)})
        upload_id = upload["id"]

        deadline = time.monotonic() + wait_s
        while True:
            if upload.get("error"):
                msg = upload["error"]
                if "duplicate" in msg.lower():
                    match = _DUPLICATE_RE.search(msg)
                    existing = next((int(g) for g in match.groups() if g), None) if match else None
                    raise DuplicateActivity(msg, existing)
                raise StravaError(f"Strava rejected the upload: {msg}")
            if upload.get("activity_id"):
                return UploadResult(int(upload["activity_id"]))
            if time.monotonic() > deadline:
                raise StravaError(f"Strava is still processing upload {upload_id}; check Strava shortly")
            time.sleep(2)
            upload = self._request("GET", f"/uploads/{upload_id}")

    def update_activity(self, activity_id: int, **fields) -> dict:
        return self._request("PUT", f"/activities/{activity_id}", json=fields)


def _save_tokens(path: Path, tokens: dict) -> None:
    keep = {k: tokens[k] for k in ("access_token", "refresh_token", "expires_at") if k in tokens}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(keep, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(path)
