"""Core workflow: poll Garmin, hold indoor rides for a distance, upload to Strava.

Kept free of Discord so it can be driven by any notifier (and tested).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Protocol

from . import fitfix
from . import store as st
from .config import Config
from .garmin import Garmin, GarminActivity, GarminAuthError
from .strava import DuplicateActivity, Strava, StravaError, UploadResult

log = logging.getLogger(__name__)

MAX_AUTO_ATTEMPTS = 5
KEY_WARNING_DAYS = 14


class ServiceError(Exception):
    """A user-facing failure message."""


class Notifier(Protocol):
    async def prompt_ride(self, activity: st.Activity) -> int | None:
        """Ask the user for a ride's distance; return the prompt's message id."""

    async def notify(self, text: str) -> None:
        """Send the user an informational message."""

    async def resolve_prompt(self, activity: st.Activity, status_line: str) -> None:
        """Mark a ride's prompt as handled (e.g. remove its buttons)."""


class Service:
    def __init__(self, cfg: Config, store: st.Store, garmin: Garmin, strava: Strava, notifier: Notifier):
        self.cfg = cfg
        self.store = store
        self.garmin = garmin
        self.strava = strava
        self.notifier = notifier

    # ------------------------------------------------------------------ #
    # Polling
    # ------------------------------------------------------------------ #

    async def poll_once(self) -> None:
        try:
            await self._poll()
        except Exception as exc:  # noqa: BLE001 - report anything, keep the loop alive
            log.exception("Poll failed")
            await self._report_poll_error(exc)
            return
        if self.store.get_kv("last_poll_error"):
            self.store.set_kv("last_poll_error", "")
            await self.notifier.notify("✅ Garmin polling is working again.")
        await self._check_key_expiry()

    async def _poll(self) -> None:
        activities = await asyncio.to_thread(self.garmin.recent_activities)

        if not self.store.get_kv("initialized"):
            for a in activities:
                self.store.add(a.id, type_key=a.type_key, name=a.name, start_gmt=a.start_gmt,
                               status=st.BASELINE)
            self.store.set_kv("initialized", "1")
            await self.notifier.notify(
                f"👋 Watching Garmin Connect every {self.cfg.poll_minutes:g} min. "
                f"Your {len(activities)} most recent activities were already there, so I'll leave them alone. "
                "Anything new from here on is mine."
            )
            return

        known = self.store.known_ids([a.id for a in activities])
        new = sorted((a for a in activities if a.id not in known), key=lambda a: a.start_gmt or "")
        for a in new:
            if a.type_key in self.cfg.indoor_types:
                self.store.add(a.id, type_key=a.type_key, name=a.name, start_gmt=a.start_gmt,
                               status=st.ERROR, summary={"start_local": a.raw.get("startTimeLocal")})
                await self._prepare_indoor(a.id)
            elif self.cfg.forward_other_activities:
                self.store.add(a.id, type_key=a.type_key, name=a.name, start_gmt=a.start_gmt,
                               status=st.ERROR)
                await self._forward(a.id)
            else:
                self.store.add(a.id, type_key=a.type_key, name=a.name, start_gmt=a.start_gmt,
                               status=st.IGNORED)

        await self._retry_errors(exclude={a.id for a in new})

    async def _retry_errors(self, exclude: set[int]) -> None:
        # Prompts that couldn't be delivered (e.g. Discord DM failed): try again.
        for a in self.store.with_status(st.PENDING):
            if a.garmin_id not in exclude and not a.discord_message_id:
                message_id = await self.notifier.prompt_ride(a)
                if message_id:
                    self.store.update(a.garmin_id, discord_message_id=message_id)
        for a in self.store.with_status(st.ERROR):
            if a.garmin_id in exclude or a.attempts >= MAX_AUTO_ATTEMPTS:
                continue
            if a.type_key in self.cfg.indoor_types:
                await self._prepare_indoor(a.garmin_id)
            else:
                await self._forward(a.garmin_id)

    async def _prepare_indoor(self, garmin_id: int) -> None:
        """Download an indoor ride, then ask the user for its distance."""
        activity = self.store.get(garmin_id)
        try:
            fit = await self._download_fit(garmin_id)
            summary = asdict(fitfix.summarize(fit))
            summary["start"] = summary["start"].isoformat() if summary["start"] else None
            summary["start_local"] = activity.summary.get("start_local")
        except Exception as exc:  # noqa: BLE001
            await self._auto_failure(activity, f"Couldn't download/read indoor ride: {exc}")
            return

        self.store.update(garmin_id, status=st.PENDING, summary=summary, last_error=None)
        message_id = await self.notifier.prompt_ride(self.store.get(garmin_id))
        if message_id:
            self.store.update(garmin_id, discord_message_id=message_id)

    async def _forward(self, garmin_id: int) -> None:
        """Upload a non-indoor activity to Strava unchanged."""
        activity = self.store.get(garmin_id)
        try:
            zip_bytes = await asyncio.to_thread(self.garmin.download, garmin_id)
            filename, data = fitfix.extract_activity_file(zip_bytes)
            data_type = _data_type(filename)
            result = await asyncio.to_thread(
                self.strava.upload, data, filename=Path(filename).name, data_type=data_type,
                name=activity.name, external_id=f"garmin-{garmin_id}",
            )
        except DuplicateActivity as exc:
            self.store.update(garmin_id, status=st.DUPLICATE, strava_activity_id=exc.existing_id,
                              last_error=str(exc))
            await self._warn_duplicate(activity)
            return
        except Exception as exc:  # noqa: BLE001
            await self._auto_failure(activity, f"Couldn't forward to Strava: {exc}")
            return
        self.store.update(garmin_id, status=st.UPLOADED, strava_activity_id=result.activity_id,
                          last_error=None)
        log.info("Forwarded %s (%s) -> %s", garmin_id, activity.type_key, result.url)

    # ------------------------------------------------------------------ #
    # User actions on a pending indoor ride
    # ------------------------------------------------------------------ #

    async def apply_fix(self, garmin_id: int, distance: float, watts: float | None) -> UploadResult:
        """Rewrite the ride with `distance` (in configured units) and upload it."""
        activity = self._claim(garmin_id)
        try:
            original = self._fit_path(garmin_id).read_bytes()
            fixed = await asyncio.to_thread(
                fitfix.fix_ride, original, distance * self.cfg.unit_m, watts, self.cfg.power_mode
            )
            result = await self._upload_indoor(activity, fixed, f"{garmin_id}-fixed.fit")
        except Exception as exc:
            raise self._restore_pending(activity, exc) from exc
        status = f"✅ Uploaded with {distance:g} {self.cfg.units}" + (f" @ {watts:g} W" if watts else "")
        await self.notifier.resolve_prompt(self.store.get(garmin_id), f"{status} → {result.url}")
        return result

    async def upload_as_is(self, garmin_id: int) -> UploadResult:
        activity = self._claim(garmin_id)
        try:
            original = self._fit_path(garmin_id).read_bytes()
            result = await self._upload_indoor(activity, original, f"{garmin_id}.fit")
        except Exception as exc:
            raise self._restore_pending(activity, exc) from exc
        await self.notifier.resolve_prompt(self.store.get(garmin_id), f"✅ Uploaded unchanged → {result.url}")
        return result

    async def ignore(self, garmin_id: int) -> None:
        self._claim(garmin_id)
        self.store.update(garmin_id, status=st.IGNORED)
        await self.notifier.resolve_prompt(self.store.get(garmin_id), "🚫 Not uploaded to Strava")

    def pending(self) -> list[st.Activity]:
        return self.store.with_status(st.PENDING)

    async def _upload_indoor(self, activity: st.Activity, data: bytes, filename: str) -> UploadResult:
        try:
            result = await asyncio.to_thread(
                self.strava.upload, data, filename=filename, data_type="fit",
                name=activity.name, trainer=True, external_id=f"garmin-{activity.garmin_id}",
            )
        except DuplicateActivity as exc:
            self.store.update(activity.garmin_id, status=st.DUPLICATE,
                              strava_activity_id=exc.existing_id, last_error=str(exc))
            link = f" (https://www.strava.com/activities/{exc.existing_id})" if exc.existing_id else ""
            raise ServiceError(
                f"Strava already has this ride{link}. Delete it on Strava, then tap the button again."
            ) from exc
        if self.cfg.strava_indoor_gear_id:
            try:
                await asyncio.to_thread(self.strava.update_activity, result.activity_id,
                                        gear_id=self.cfg.strava_indoor_gear_id)
            except StravaError:
                log.exception("Uploaded, but setting gear failed")
        self.store.update(activity.garmin_id, status=st.UPLOADED,
                          strava_activity_id=result.activity_id, last_error=None)
        return result

    def _claim(self, garmin_id: int) -> st.Activity:
        # DUPLICATE is claimable so the user can retry after deleting on Strava.
        if not self.store.claim(garmin_id, (st.PENDING, st.DUPLICATE)):
            activity = self.store.get(garmin_id)
            state = activity.status if activity else "unknown"
            raise ServiceError(f"That ride isn't waiting for input (it's {state}).")
        return self.store.get(garmin_id)

    def _restore_pending(self, activity: st.Activity, exc: Exception) -> Exception:
        current = self.store.get(activity.garmin_id)
        if current and current.status == st.PROCESSING:
            self.store.update(activity.garmin_id, status=st.PENDING, last_error=str(exc))
        if isinstance(exc, ServiceError):
            return exc
        if isinstance(exc, (fitfix.FitError, StravaError)):
            return ServiceError(str(exc))
        log.exception("Unexpected failure handling %s", activity.garmin_id, exc_info=exc)
        return ServiceError(f"Something went wrong: {exc}")

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _fit_path(self, garmin_id: int) -> Path:
        return self.cfg.fit_dir / f"{garmin_id}.fit"

    async def _download_fit(self, garmin_id: int) -> bytes:
        path = self._fit_path(garmin_id)
        if path.exists():
            return path.read_bytes()
        zip_bytes = await asyncio.to_thread(self.garmin.download, garmin_id)
        filename, data = fitfix.extract_activity_file(zip_bytes)
        if _data_type(filename) != "fit":
            raise fitfix.FitError(f"Garmin returned {filename}, not a FIT file")
        path.write_bytes(data)
        return data

    async def _auto_failure(self, activity: st.Activity, message: str) -> None:
        attempts = activity.attempts + 1
        self.store.update(activity.garmin_id, status=st.ERROR, attempts=attempts, last_error=message)
        log.warning("%s (%s, attempt %d): %s", activity.garmin_id, activity.name, attempts, message)
        if attempts == MAX_AUTO_ATTEMPTS:
            await self.notifier.notify(
                f"⚠️ Gave up on **{activity.name or activity.garmin_id}** after {attempts} tries: {message}"
            )

    async def _warn_duplicate(self, activity: st.Activity) -> None:
        if self.store.get_kv("duplicate_warned"):
            return
        self.store.set_kv("duplicate_warned", "1")
        await self.notifier.notify(
            f"ℹ️ Strava already had **{activity.name}**, so Garmin's own Strava sync is probably still on. "
            "Turn it off (Garmin Connect → Settings → Connected Apps → Strava → Disconnect) or indoor "
            "rides will reach Strava before I can fix them. I'll only mention this once."
        )

    async def _report_poll_error(self, exc: Exception) -> None:
        text = str(exc) if isinstance(exc, GarminAuthError) else f"{type(exc).__name__}: {exc}"
        if self.store.get_kv("last_poll_error") == text:
            return
        self.store.set_kv("last_poll_error", text)
        await self.notifier.notify(f"⚠️ Garmin polling failed, will keep retrying: {text}")

    async def _check_key_expiry(self) -> None:
        expires = self.cfg.garmin_key_expires
        if not expires:
            return
        days = (expires - date.today()).days
        today = date.today().isoformat()
        if days > KEY_WARNING_DAYS or self.store.get_kv("key_warning_date") == today:
            return
        self.store.set_kv("key_warning_date", today)
        when = "has expired" if days < 0 else f"expires in {days} day(s) ({expires})"
        await self.notifier.notify(
            f"🔑 Your Garmin Personal API key {when}. Create a new one at "
            "https://connect.garmin.com/app/settings/personalApiToken, then update GARMIN_API_KEY "
            "and GARMIN_KEY_EXPIRES in .env and restart."
        )


def _data_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower().lstrip(".")
    return {"fit": "fit", "tcx": "tcx", "gpx": "gpx"}.get(suffix, "fit")
