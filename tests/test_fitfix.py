import io
import zipfile

import pytest
from garmin_fit_sdk import Decoder, Stream

from indoor_rides import fitfix
from tests.fixtures import indoor_ride, smart_recorded_ride

MILE = 1609.344


def decode(data: bytes) -> dict:
    messages, errors = Decoder(Stream.from_byte_array(bytearray(data))).read()
    assert not errors
    return messages


def test_distance_is_spread_by_cadence():
    out = fitfix.fix_ride(indoor_ride(), distance_m=5 * MILE)
    m = decode(out)
    records = m["record_mesgs"]

    assert records[-1]["distance"] == pytest.approx(5 * MILE, abs=1)
    assert m["session_mesgs"][0]["total_distance"] == pytest.approx(5 * MILE, abs=1)
    laps = m["lap_mesgs"]
    assert sum(l["total_distance"] for l in laps) == pytest.approx(5 * MILE, abs=1)

    # Distance never goes backwards and stays flat while coasting (cadence 0).
    dists = [r["distance"] for r in records]
    assert dists == sorted(dists)
    coast = [r for r in records if r["cadence"] == 0]
    assert coast and coast[0]["distance"] == pytest.approx(coast[-1]["distance"], abs=0.01)
    assert all(r["enhanced_speed"] == 0 for r in coast)


def test_original_data_is_preserved():
    original = decode(indoor_ride())
    fixed = decode(fitfix.fix_ride(indoor_ride(), distance_m=8000))
    assert [r["heart_rate"] for r in fixed["record_mesgs"]] == [
        r["heart_rate"] for r in original["record_mesgs"]
    ]
    assert fixed["file_id_mesgs"][0]["manufacturer"] == "garmin"
    assert fixed["session_mesgs"][0]["sub_sport"] == "indoor_cycling"
    assert len(fixed["lap_mesgs"]) == 2


def test_no_cadence_spreads_evenly():
    out = fitfix.fix_ride(indoor_ride(cadence=False), distance_m=6000)
    records = decode(out)["record_mesgs"]
    speeds = {round(r["enhanced_speed"], 2) for r in records}
    assert len(speeds) == 1
    assert records[-1]["distance"] == pytest.approx(6000, abs=1)


def test_constant_power():
    out = fitfix.fix_ride(indoor_ride(), distance_m=8000, avg_watts=185)
    m = decode(out)
    assert {r["power"] for r in m["record_mesgs"]} == {185}
    assert m["session_mesgs"][0]["avg_power"] == 185
    assert m["session_mesgs"][0]["normalized_power"] == 185


def test_cadence_power_averages_to_target():
    out = fitfix.fix_ride(indoor_ride(), distance_m=8000, avg_watts=200, power_mode="cadence")
    m = decode(out)
    powers = [r["power"] for r in m["record_mesgs"]]
    assert sum(powers) / len(powers) == pytest.approx(200, abs=1)
    assert min(powers) == 0  # coasting
    assert m["session_mesgs"][0]["avg_power"] == pytest.approx(200, abs=1)


def test_existing_power_kept_when_no_watts_given():
    out = fitfix.fix_ride(indoor_ride(power=True), distance_m=8000)
    assert {r["power"] for r in decode(out)["record_mesgs"]} == {150}


def test_summarize():
    s = fitfix.summarize(indoor_ride())
    assert s.duration_s == 600
    assert s.has_cadence and not s.has_power
    assert s.sub_sport == "indoor_cycling"
    assert s.avg_hr == 135


def test_extract_from_zip():
    fit = indoor_ride()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("12345_ACTIVITY.fit", fit)
    name, data = fitfix.extract_activity_file(buf.getvalue())
    assert name.endswith(".fit") and data == fit
    assert fitfix.extract_activity_file(fit)[1] == fit


@pytest.mark.parametrize("pause", [True, False])
def test_real_garmin_quirks(pause):
    """Smart recording + summaries stamped at the start time (seen in real files)."""
    out = fitfix.fix_ride(smart_recorded_ride(pause=pause), distance_m=10 * MILE, avg_watts=180)
    m = decode(out)
    session = m["session_mesgs"][0]
    assert session["total_distance"] == pytest.approx(10 * MILE, abs=1)
    laps = m["lap_mesgs"]
    assert sum(l["total_distance"] for l in laps) == pytest.approx(10 * MILE, abs=1)
    # Laps split roughly by riding time (lap 1 = first 900 s of ~1680-1800 s).
    assert 0.45 < laps[0]["total_distance"] / session["total_distance"] < 0.6

    # Even speed while riding regardless of record spacing; nothing added during the pause.
    records = m["record_mesgs"]
    speeds = [r["enhanced_speed"] for r in records[1:]]
    moving = [s for s in speeds if s > 0]
    assert max(moving) == pytest.approx(min(moving), rel=1e-3)
    assert len(moving) == len(speeds)
    # Record speed == distance / timer time, so the 120 s pause wasn't counted as riding.
    assert session["enhanced_avg_speed"] == pytest.approx(moving[0], rel=0.005)

    assert session["avg_power"] == 180 and session["normalized_power"] == 180
    assert fitfix.summarize(out).distance_m == pytest.approx(10 * MILE, abs=1)


def test_nan_fields_are_dropped():
    # Real Garmin indoor rides decode unused float32 session fields (grit/flow) as NaN.
    decoded = fitfix._decode(indoor_ride())
    session = next(m for n, m in decoded.mesgs if n == fitfix.SESSION)
    session.update(total_grit=float("nan"), avg_flow=float("inf"))
    session["developer_fields"] = {}
    m = decode(fitfix._encode(decoded))
    assert "total_grit" not in m["session_mesgs"][0]
    assert m["session_mesgs"][0]["sub_sport"] == "indoor_cycling"


def test_rejects_bad_input():
    with pytest.raises(fitfix.FitError):
        fitfix.fix_ride(b"not a fit file at all", distance_m=1000)
    with pytest.raises(fitfix.FitError):
        fitfix.fix_ride(indoor_ride(), distance_m=0)
