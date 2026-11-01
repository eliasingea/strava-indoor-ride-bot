"""Build synthetic indoor-ride FIT files for tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from garmin_fit_sdk import Encoder, Profile

MESG = Profile["mesg_num"]
START = datetime(2026, 10, 30, 11, 0, 0, tzinfo=UTC)


def indoor_ride(seconds: int = 600, *, cadence: bool = True, power: bool = False) -> bytes:
    """A ride with HR and cadence, a 60 s coast in the middle, and two laps."""
    enc = Encoder()
    enc.on_mesg(MESG["FILE_ID"], {
        "type": "activity", "manufacturer": "garmin", "product": 3121,
        "serial_number": 1234567890, "time_created": START,
    })
    enc.on_mesg(MESG["DEVICE_INFO"], {
        "timestamp": START, "device_index": "creator", "manufacturer": "garmin",
        "product": 3121, "software_version": 9.1,
    })
    enc.on_mesg(MESG["EVENT"], {"timestamp": START, "event": "timer", "event_type": "start"})

    half = seconds // 2
    for i in range(seconds):
        record = {"timestamp": START + timedelta(seconds=i), "heart_rate": 120 + i % 30}
        if cadence:
            record["cadence"] = 0 if half <= i < half + 60 else 85 + i % 10
        if power:
            record["power"] = 150
        enc.on_mesg(MESG["RECORD"], record)

    end = START + timedelta(seconds=seconds - 1)
    mid = START + timedelta(seconds=half - 1)
    enc.on_mesg(MESG["EVENT"], {"timestamp": end, "event": "timer", "event_type": "stop_all"})
    for lap_start, lap_end in ((START, mid), (mid + timedelta(seconds=1), end)):
        t = (lap_end - lap_start).total_seconds() + 1
        enc.on_mesg(MESG["LAP"], {
            "timestamp": lap_end, "start_time": lap_start, "total_timer_time": t,
            "total_elapsed_time": t, "event": "lap", "event_type": "stop",
            "sport": "cycling", "sub_sport": "indoor_cycling",
        })
    enc.on_mesg(MESG["SESSION"], {
        "timestamp": end, "start_time": START, "total_timer_time": float(seconds),
        "total_elapsed_time": float(seconds), "sport": "cycling", "sub_sport": "indoor_cycling",
        "avg_heart_rate": 135, "event": "lap", "event_type": "stop", "num_laps": 2,
        "first_lap_index": 0,
    })
    enc.on_mesg(MESG["ACTIVITY"], {
        "timestamp": end, "total_timer_time": float(seconds), "num_sessions": 1,
        "type": "manual", "event": "activity", "event_type": "stop",
    })
    return enc.close()


def smart_recorded_ride(*, pause: bool = True) -> bytes:
    """Mimics a real Garmin watch file: records every 1-8 s ("smart recording"),
    no cadence, every lap/session/activity timestamp equal to the start time,
    and (optionally) a 120 s pause with timer stop/start events."""
    enc = Encoder()
    enc.on_mesg(MESG["FILE_ID"], {"type": "activity", "manufacturer": "garmin", "product": 4315,
                                  "time_created": START})
    enc.on_mesg(MESG["EVENT"], {"timestamp": START, "event": "timer", "event_type": "start"})

    gaps = [1, 2, 3, 1, 5, 8, 1, 4]
    pause_at, pause_len = 600, 120
    t, i = 0, 0
    lap2_start = None
    while t < 1800:
        ts = START + timedelta(seconds=t)
        enc.on_mesg(MESG["RECORD"], {"timestamp": ts, "heart_rate": 130, "distance": 0.0})
        if lap2_start is None and t >= 900:
            lap2_start = ts
        gap = gaps[i % len(gaps)]
        i += 1
        if pause and pause_at - gap < t <= pause_at:
            enc.on_mesg(MESG["EVENT"], {"timestamp": ts, "event": "timer", "event_type": "stop_all"})
            enc.on_mesg(MESG["EVENT"], {"timestamp": ts + timedelta(seconds=pause_len),
                                        "event": "timer", "event_type": "start"})
            t += pause_len
        t += gap
    last = START + timedelta(seconds=t - gap)
    enc.on_mesg(MESG["EVENT"], {"timestamp": last, "event": "timer", "event_type": "stop_all"})

    elapsed = (last - START).total_seconds()
    timer = elapsed - (pause_len if pause else 0)
    lap1 = (lap2_start - START).total_seconds()
    for lap_start, lap_time in ((START, lap1), (lap2_start, timer - lap1)):
        enc.on_mesg(MESG["LAP"], {"timestamp": START, "start_time": lap_start,
                                  "total_timer_time": lap_time, "total_distance": 0.0,
                                  "sport": "cycling", "sub_sport": "indoor_cycling"})
    enc.on_mesg(MESG["SESSION"], {"timestamp": START, "start_time": START, "total_timer_time": timer,
                                  "total_elapsed_time": elapsed, "total_distance": 0.0,
                                  "sport": "cycling", "sub_sport": "indoor_cycling"})
    enc.on_mesg(MESG["ACTIVITY"], {"timestamp": START, "total_timer_time": timer, "num_sessions": 1})
    return enc.close()
