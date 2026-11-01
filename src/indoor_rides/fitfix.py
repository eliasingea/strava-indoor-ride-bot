"""Rewrite the distance (and optionally power) of an indoor ride FIT file.

The original file is decoded message by message and re-encoded with Garmin's
official FIT SDK, so everything Strava cares about (device, heart rate,
cadence, laps, timestamps) is preserved. Only distance/speed fields, and power
fields when an average wattage is supplied, are replaced.

Distance is spread across the ride by pedalling: each record's share of the
total is proportional to cadence x elapsed seconds (or just elapsed seconds
when the file has no cadence). Coasting therefore adds no distance, and speed
tracks cadence, which is close to how a real trainer behaves.
"""

from __future__ import annotations

import io
import logging
import math
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from garmin_fit_sdk import Decoder, Encoder, Profile, Stream

log = logging.getLogger(__name__)

MESG = Profile["mesg_num"]
FILE_ID = MESG["FILE_ID"]
RECORD = MESG["RECORD"]
LAP = MESG["LAP"]
EVENT = MESG["EVENT"]
SESSION = MESG["SESSION"]

# Without timer events in the file, a gap longer than this is taken as a pause.
FALLBACK_MAX_GAP_S = 30.0

PowerMode = Literal["constant", "cadence"]

Mesg = dict[str, Any]


class FitError(Exception):
    """The file could not be decoded, fixed or re-encoded."""


@dataclass
class RideSummary:
    start: datetime | None
    duration_s: float
    distance_m: float
    avg_hr: float | None
    avg_cadence: float | None
    avg_power: float | None
    has_cadence: bool
    has_power: bool
    sport: str | None
    sub_sport: str | None


@dataclass
class _Decoded:
    mesgs: list[tuple[int, Mesg]]
    field_descriptions: dict[int, dict[str, Mesg]]


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def extract_activity_file(zip_bytes: bytes) -> tuple[str, bytes]:
    """Return (filename, contents) of the activity file inside a Garmin download ZIP.

    Garmin's download endpoint returns a ZIP that normally holds a single
    ``.fit`` file. Plain FIT bytes are passed through unchanged.
    """
    if _looks_like_fit(zip_bytes):
        return "activity.fit", zip_bytes
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if not names:
                raise FitError("Downloaded ZIP is empty")
            fit = [n for n in names if n.lower().endswith(".fit")]
            name = fit[0] if fit else names[0]
            return name, zf.read(name)
    except zipfile.BadZipFile as exc:
        raise FitError("Download is neither a ZIP nor a FIT file") from exc


def summarize(fit_bytes: bytes) -> RideSummary:
    decoded = _decode(fit_bytes)
    records = [m for n, m in decoded.mesgs if n == RECORD and m.get("timestamp")]
    sessions = [m for n, m in decoded.mesgs if n == SESSION]
    session = sessions[0] if sessions else {}

    start = session.get("start_time") or (records[0]["timestamp"] if records else None)
    duration = session.get("total_timer_time") or session.get("total_elapsed_time")
    if duration is None and len(records) > 1:
        duration = (records[-1]["timestamp"] - records[0]["timestamp"]).total_seconds()

    hr = [r["heart_rate"] for r in records if r.get("heart_rate")]
    cad = [r["cadence"] for r in records if r.get("cadence") is not None]
    pwr = [r["power"] for r in records if r.get("power") is not None]

    return RideSummary(
        start=start,
        duration_s=float(duration or 0),
        distance_m=float(session.get("total_distance") or 0),
        avg_hr=session.get("avg_heart_rate") or (sum(hr) / len(hr) if hr else None),
        avg_cadence=session.get("avg_cadence") or (sum(cad) / len(cad) if cad else None),
        avg_power=session.get("avg_power") or (sum(pwr) / len(pwr) if pwr else None),
        has_cadence=any(c > 0 for c in cad),
        has_power=any(p > 0 for p in pwr),
        sport=_as_str(session.get("sport")),
        sub_sport=_as_str(session.get("sub_sport")),
    )


def fix_ride(
    fit_bytes: bytes,
    distance_m: float,
    avg_watts: float | None = None,
    power_mode: PowerMode = "constant",
) -> bytes:
    """Return a new FIT file with the given total distance (and average power)."""
    if distance_m <= 0:
        raise FitError("Distance must be greater than zero")
    if avg_watts is not None and avg_watts <= 0:
        raise FitError("Average watts must be greater than zero")

    decoded = _decode(fit_bytes)
    records = [m for n, m in decoded.mesgs if n == RECORD and m.get("timestamp")]
    if len(records) < 2:
        raise FitError("Activity has fewer than two data records; nothing to rewrite")
    records.sort(key=lambda r: r["timestamp"])

    dts = _record_durations(records, _timer_pauses(decoded.mesgs))
    has_cadence = any((r.get("cadence") or 0) > 0 for r in records)

    steps = _apply_distance(records, dts, distance_m, has_cadence)
    if avg_watts is not None:
        _apply_power(records, dts, avg_watts, power_mode if has_cadence else "constant")

    rows = list(zip(records, dts, steps))
    power_changed = avg_watts is not None
    for mesg, members in _assign_laps(decoded.mesgs, rows):
        _apply_summary(mesg, members, power_changed=power_changed)
    sessions = [m for n, m in decoded.mesgs if n == SESSION]
    for mesg in sessions:
        members = rows if len(sessions) == 1 else _in_window(mesg, rows)
        _apply_summary(mesg, members, power_changed=power_changed)

    return _encode(decoded)


# --------------------------------------------------------------------------- #
# Rewriting
# --------------------------------------------------------------------------- #

Row = tuple[Mesg, float, float]  # (record, seconds it represents, metres it adds)


def _timer_pauses(mesgs: list[tuple[int, Mesg]]) -> list[tuple[datetime, datetime]] | None:
    """(stopped, restarted) intervals from timer events, or None if the file has none."""
    events = sorted(
        (m for n, m in mesgs
         if n == EVENT and _as_str(m.get("event")) == "timer" and m.get("timestamp")),
        key=lambda m: m["timestamp"],
    )
    if not events:
        return None
    pauses, stopped_at = [], None
    for e in events:
        kind = _as_str(e.get("event_type")) or ""
        if kind.startswith("stop") and stopped_at is None:
            stopped_at = e["timestamp"]
        elif kind == "start" and stopped_at is not None:
            pauses.append((stopped_at, e["timestamp"]))
            stopped_at = None
    return pauses


def _record_durations(records: list[Mesg], pauses: list[tuple[datetime, datetime]] | None) -> list[float]:
    """Seconds of riding each record represents: the gap since the previous
    record, minus any time the timer was stopped. Devices using "smart
    recording" log every 1-15 s, so gaps are normal. Without timer events,
    unusually long gaps are assumed to be pauses."""
    dts = [1.0]  # the first record stands for one second, like the others
    for prev, cur in zip(records, records[1:]):
        a, b = prev["timestamp"], cur["timestamp"]
        gap = max((b - a).total_seconds(), 0.0)
        if pauses is None:
            gap = min(gap, FALLBACK_MAX_GAP_S)
        else:
            for stop, start in pauses:
                overlap = (min(b, start) - max(a, stop)).total_seconds()
                if overlap > 0:
                    gap -= overlap
        dts.append(max(gap, 0.0))
    return dts


def _apply_distance(records: list[Mesg], dts: list[float], total_m: float, has_cadence: bool) -> list[float]:
    weights = [dt * ((r.get("cadence") or 0) if has_cadence else 1.0) for r, dt in zip(records, dts)]
    total_weight = sum(weights)
    if total_weight <= 0:  # cadence present but always zero; fall back to time
        weights = list(dts)
        total_weight = sum(weights)

    steps = [total_m * w / total_weight for w in weights]
    cumulative = 0.0
    for record, step, dt in zip(records, steps, dts):
        cumulative += step
        speed = step / dt if dt > 0 else 0.0
        record["distance"] = cumulative
        record["speed"] = speed
        record["enhanced_speed"] = speed
    records[-1]["distance"] = total_m  # remove float drift
    return steps


def _apply_power(records: list[Mesg], dts: list[float], avg_watts: float, mode: PowerMode) -> None:
    if mode == "cadence":
        total_time = sum(dts)
        mean_cadence = sum((r.get("cadence") or 0) * dt for r, dt in zip(records, dts)) / total_time
        for record in records:
            record["power"] = round(avg_watts * (record.get("cadence") or 0) / mean_cadence)
    else:
        for record in records:
            record["power"] = round(avg_watts)


def _assign_laps(mesgs: list[tuple[int, Mesg]], rows: list[Row]) -> list[tuple[Mesg, list[Row]]]:
    """Give each record to exactly one lap: the last lap starting at or before it.

    Lap end timestamps aren't trusted (some Garmin files stamp every lap with
    the activity start time), so only start times are used.
    """
    laps = sorted(
        (m for n, m in mesgs if n == LAP and m.get("start_time")), key=lambda m: m["start_time"]
    )
    assigned: list[tuple[Mesg, list[Row]]] = [(lap, []) for lap in laps]
    for row in rows:
        idx = 0
        for i, lap in enumerate(laps):
            if lap["start_time"] <= row[0]["timestamp"]:
                idx = i
        if assigned:
            assigned[idx][1].append(row)
    return assigned


def _in_window(mesg: Mesg, rows: list[Row]) -> list[Row]:
    start = mesg.get("start_time")
    elapsed = mesg.get("total_elapsed_time")
    end = start + timedelta(seconds=elapsed + 1) if start and elapsed else mesg.get("timestamp")
    return [
        r for r in rows
        if (start is None or r[0]["timestamp"] >= start) and (end is None or r[0]["timestamp"] <= end)
    ]


def _apply_summary(mesg: Mesg, rows: list[Row], *, power_changed: bool) -> None:
    """Recompute a lap or session's distance/speed (and power) from its records."""
    if not rows:
        return
    distance = sum(step for _, _, step in rows)
    moving = sum(dt for _, dt, _ in rows)
    timer = mesg.get("total_timer_time") or moving
    avg_speed = distance / timer if timer else 0.0
    max_speed = max(avg_speed, max(r["speed"] for r, _, _ in rows))

    mesg["total_distance"] = distance
    for key, value in (("avg_speed", avg_speed), ("max_speed", max_speed)):
        mesg[key] = value
        mesg[f"enhanced_{key}"] = value

    if power_changed:
        work = sum(r["power"] * dt for r, dt, _ in rows)
        # Expand to a 1 Hz series so smart-recorded files weight power by time.
        series = [r["power"] for r, dt, _ in rows for _ in range(max(1, round(dt)))]
        mesg["avg_power"] = round(work / moving) if moving else rows[0][0]["power"]
        mesg["max_power"] = max(r["power"] for r, _, _ in rows)
        mesg["normalized_power"] = round(_normalized_power(series))
        mesg["total_work"] = round(work)


def _normalized_power(powers: list[int]) -> float:
    """Coggan normalized power, assuming roughly 1 s records."""
    window = 30
    if len(powers) < window:
        return sum(powers) / len(powers)
    rolling = []
    running = sum(powers[:window])
    rolling.append(running / window)
    for i in range(window, len(powers)):
        running += powers[i] - powers[i - window]
        rolling.append(running / window)
    return (sum(p**4 for p in rolling) / len(rolling)) ** 0.25


# --------------------------------------------------------------------------- #
# Decode / encode
# --------------------------------------------------------------------------- #


def _decode(fit_bytes: bytes) -> _Decoded:
    if not _looks_like_fit(fit_bytes):
        raise FitError("Not a FIT file")

    mesgs: list[tuple[int, Mesg]] = []
    field_descriptions: dict[int, dict[str, Mesg]] = {}

    def on_mesg(num: int, mesg: Mesg) -> None:
        mesgs.append((num, mesg))

    def on_field_description(key: int, dev_data_id: Mesg, field_desc: Mesg) -> None:
        field_descriptions[key] = {
            "developer_data_id_mesg": dev_data_id,
            "field_description_mesg": field_desc,
        }

    decoder = Decoder(Stream.from_byte_array(bytearray(fit_bytes)))
    # Keep values as close to what is stored as possible so the re-encode is
    # faithful: no component expansion, sub-field aliases or HR merging.
    _, errors = decoder.read(
        expand_sub_fields=False,
        expand_components=False,
        merge_heart_rates=False,
        mesg_listener=on_mesg,
        field_description_listener=on_field_description,
    )
    if errors:
        raise FitError(f"Could not decode FIT file: {errors[0]}")
    if not any(n == FILE_ID for n, _ in mesgs):
        raise FitError("FIT file has no file_id message")
    return _Decoded(mesgs, field_descriptions)


def _encode(decoded: _Decoded) -> bytes:
    try:
        encoder = Encoder(field_descriptions=decoded.field_descriptions)
    except ValueError:
        log.warning("Developer field descriptions unusable; dropping developer fields")
        decoded.field_descriptions = {}
        encoder = Encoder()

    dropped: dict[int, int] = {}
    for num, mesg in decoded.mesgs:
        if num not in Profile["messages"]:
            dropped[num] = dropped.get(num, 0) + 1  # undocumented Garmin message
            continue
        mesg = _drop_invalid_values(mesg)
        if not decoded.field_descriptions:
            mesg.pop("developer_fields", None)
        try:
            encoder.on_mesg(num, mesg)
        except ValueError as exc:
            if num in (FILE_ID, RECORD, LAP, SESSION):
                raise FitError(f"Could not encode message {num}: {exc}") from exc
            dropped[num] = dropped.get(num, 0) + 1

    if dropped:
        log.info("Dropped messages not re-encodable (mesg_num: count): %s", dropped)
    return encoder.close()


def _drop_invalid_values(mesg: Mesg) -> Mesg:
    """Remove fields whose value is NaN/inf.

    Garmin marks unused float fields (e.g. grit/flow on an indoor ride) with the
    FIT "invalid" bit pattern, which decodes to NaN. The encoder refuses NaN, and
    an absent field means the same thing as an invalid one, so drop it.
    """
    def ok(value: Any) -> bool:
        if isinstance(value, float):
            return math.isfinite(value)
        if isinstance(value, list):
            return all(ok(v) for v in value)
        return True

    clean = {k: v for k, v in mesg.items() if k == "developer_fields" or ok(v)}
    dev = mesg.get("developer_fields")
    if isinstance(dev, dict):
        clean["developer_fields"] = {k: v for k, v in dev.items() if ok(v)}
    return clean


def _looks_like_fit(data: bytes) -> bool:
    return len(data) >= 12 and data[8:12] == b".FIT"


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)
