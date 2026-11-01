"""SQLite state: which Garmin activities we've seen and what happened to them."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

# Status values
BASELINE = "baseline"        # existed before the service started; never touched
PENDING = "pending"          # indoor ride waiting for the user's distance
PROCESSING = "processing"    # being fixed / uploaded right now
UPLOADED = "uploaded"        # on Strava (fixed or forwarded)
IGNORED = "ignored"          # user chose not to upload
DUPLICATE = "duplicate"      # Strava already had it
ERROR = "error"              # failed; will be retried

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    garmin_id           INTEGER PRIMARY KEY,
    type_key            TEXT,
    name                TEXT,
    start_gmt           TEXT,
    status              TEXT NOT NULL,
    summary             TEXT,
    discord_message_id  INTEGER,
    strava_activity_id  INTEGER,
    attempts            INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    updated_at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS activities_status ON activities(status);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


@dataclass
class Activity:
    garmin_id: int
    type_key: str | None
    name: str | None
    start_gmt: str | None
    status: str
    summary: dict[str, Any]
    discord_message_id: int | None
    strava_activity_id: int | None
    attempts: int
    last_error: str | None


class Store:
    def __init__(self, path: Path):
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # key/value ----------------------------------------------------------- #

    def get_kv(self, key: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_kv(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (key, value))

    # activities ---------------------------------------------------------- #

    def known_ids(self, ids: list[int]) -> set[int]:
        if not ids:
            return set()
        marks = ",".join("?" * len(ids))
        with self._conn() as c:
            rows = c.execute(f"SELECT garmin_id FROM activities WHERE garmin_id IN ({marks})", ids)
            return {r["garmin_id"] for r in rows}

    def add(self, garmin_id: int, *, type_key: str | None, name: str | None,
            start_gmt: str | None, status: str, summary: dict[str, Any] | None = None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO activities"
                "(garmin_id, type_key, name, start_gmt, status, summary, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (garmin_id, type_key, name, start_gmt, status, json.dumps(summary or {}), time.time()),
            )

    def get(self, garmin_id: int) -> Activity | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM activities WHERE garmin_id = ?", (garmin_id,)).fetchone()
        return _row(row) if row else None

    def by_discord_message(self, message_id: int) -> Activity | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM activities WHERE discord_message_id = ?", (message_id,)
            ).fetchone()
        return _row(row) if row else None

    def with_status(self, *statuses: str) -> list[Activity]:
        marks = ",".join("?" * len(statuses))
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM activities WHERE status IN ({marks}) ORDER BY start_gmt", statuses
            ).fetchall()
        return [_row(r) for r in rows]

    def update(self, garmin_id: int, **fields: Any) -> None:
        if "summary" in fields:
            fields["summary"] = json.dumps(fields["summary"])
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE activities SET {sets} WHERE garmin_id = ?", (*fields.values(), garmin_id))

    def claim(self, garmin_id: int, from_statuses: tuple[str, ...]) -> bool:
        """Atomically move an activity to PROCESSING if it's in one of from_statuses."""
        marks = ",".join("?" * len(from_statuses))
        with self._conn() as c:
            cur = c.execute(
                f"UPDATE activities SET status = ?, updated_at = ? "
                f"WHERE garmin_id = ? AND status IN ({marks})",
                (PROCESSING, time.time(), garmin_id, *from_statuses),
            )
            return cur.rowcount == 1


def _row(row: sqlite3.Row) -> Activity:
    return Activity(
        garmin_id=row["garmin_id"],
        type_key=row["type_key"],
        name=row["name"],
        start_gmt=row["start_gmt"],
        status=row["status"],
        summary=json.loads(row["summary"] or "{}"),
        discord_message_id=row["discord_message_id"],
        strava_activity_id=row["strava_activity_id"],
        attempts=row["attempts"],
        last_error=row["last_error"],
    )
