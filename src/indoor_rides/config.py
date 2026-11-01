"""Settings loaded from environment variables (or a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

MILE_M = 1609.344
KM_M = 1000.0


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    data_dir: Path
    garmin_api_key: str | None
    garmin_key_expires: date | None
    strava_client_id: str | None
    strava_client_secret: str | None
    discord_bot_token: str | None
    discord_user_id: int | None
    poll_minutes: float
    indoor_types: frozenset[str]
    forward_other_activities: bool
    units: str  # "mi" or "km"
    power_mode: str  # "constant" or "cadence"
    strava_indoor_gear_id: str | None

    @property
    def unit_m(self) -> float:
        return MILE_M if self.units == "mi" else KM_M

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def fit_dir(self) -> Path:
        return self.data_dir / "fit"

    @property
    def strava_tokens_path(self) -> Path:
        return self.data_dir / "strava-tokens.json"

    @property
    def garmin_token_cache(self) -> Path:
        return self.data_dir / "garmin-bearer-token.json"

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            env = ", ".join(n.upper() for n in missing)
            raise ConfigError(f"Missing required setting(s): {env}. Set them in .env")


def load(env_file: str | None = None) -> Config:
    load_dotenv(env_file or ".env")

    units = _get("DISTANCE_UNITS", "mi").lower()
    if units not in ("mi", "km"):
        raise ConfigError("DISTANCE_UNITS must be 'mi' or 'km'")
    power_mode = _get("POWER_MODE", "constant").lower()
    if power_mode not in ("constant", "cadence"):
        raise ConfigError("POWER_MODE must be 'constant' or 'cadence'")

    user_id = _get("DISCORD_USER_ID")
    expires = _get("GARMIN_KEY_EXPIRES")

    data_dir = Path(_get("DATA_DIR", "data")).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "fit").mkdir(exist_ok=True)

    return Config(
        data_dir=data_dir,
        garmin_api_key=_get("GARMIN_API_KEY"),
        garmin_key_expires=date.fromisoformat(expires) if expires else None,
        strava_client_id=_get("STRAVA_CLIENT_ID"),
        strava_client_secret=_get("STRAVA_CLIENT_SECRET"),
        discord_bot_token=_get("DISCORD_BOT_TOKEN"),
        discord_user_id=int(user_id) if user_id else None,
        poll_minutes=float(_get("POLL_MINUTES", "5")),
        indoor_types=frozenset(
            t.strip() for t in _get("INDOOR_ACTIVITY_TYPES", "indoor_cycling").split(",") if t.strip()
        ),
        forward_other_activities=_get("FORWARD_OTHER_ACTIVITIES", "true").lower() in ("1", "true", "yes"),
        units=units,
        power_mode=power_mode,
        strava_indoor_gear_id=_get("STRAVA_INDOOR_GEAR_ID"),
    )


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or default
