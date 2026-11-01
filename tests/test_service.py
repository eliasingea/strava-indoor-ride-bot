import asyncio
import io
import zipfile
from dataclasses import replace

import pytest
from garmin_fit_sdk import Decoder, Stream

from indoor_rides import config as config_mod
from indoor_rides import store as st
from indoor_rides.bot import parse_reply
from indoor_rides.garmin import GarminActivity
from indoor_rides.service import Service, ServiceError
from indoor_rides.strava import DuplicateActivity, UploadResult
from tests.fixtures import indoor_ride


def zipped(fit: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("activity.fit", fit)
    return buf.getvalue()


def act(id_, type_key, start):
    return GarminActivity(id_, f"Ride {id_}", type_key, start, {"startTimeLocal": start})


class FakeGarmin:
    def __init__(self):
        self.activities = []
        self.downloads = 0

    def recent_activities(self):
        return list(self.activities)

    def download(self, activity_id):
        self.downloads += 1
        return zipped(indoor_ride())


class FakeStrava:
    def __init__(self):
        self.uploads = []
        self.duplicate = False
        self.next_id = 1000

    def upload(self, data, **kw):
        if self.duplicate:
            raise DuplicateActivity("duplicate of activity 42", 42)
        self.uploads.append((data, kw))
        self.next_id += 1
        return UploadResult(self.next_id)

    def update_activity(self, activity_id, **fields):
        self.gear = (activity_id, fields)


class FakeNotifier:
    def __init__(self):
        self.prompts, self.messages, self.resolved = [], [], []

    async def prompt_ride(self, activity):
        self.prompts.append(activity)
        return 555

    async def notify(self, text):
        self.messages.append(text)

    async def resolve_prompt(self, activity, status_line):
        self.resolved.append(status_line)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STRAVA_INDOOR_GEAR_ID", "b123")
    cfg = config_mod.load(str(tmp_path / "none.env"))
    garmin, strava, notifier = FakeGarmin(), FakeStrava(), FakeNotifier()
    svc = Service(cfg, st.Store(cfg.db_path), garmin, strava, notifier)
    return svc, garmin, strava, notifier


def run(coro):
    return asyncio.run(coro)


def test_first_poll_baselines_existing(env):
    svc, garmin, strava, notifier = env
    garmin.activities = [act(1, "indoor_cycling", "2026-10-01 10:00:00")]
    run(svc.poll_once())
    assert svc.store.get(1).status == st.BASELINE
    assert not notifier.prompts and not strava.uploads
    assert "leave them alone" in notifier.messages[0]


def test_indoor_ride_prompt_then_fix(env):
    svc, garmin, strava, notifier = env
    run(svc.poll_once())  # baseline (empty)
    garmin.activities = [act(2, "indoor_cycling", "2026-10-30 11:00:00")]
    run(svc.poll_once())

    a = svc.store.get(2)
    assert a.status == st.PENDING and a.discord_message_id == 555
    assert notifier.prompts[0].summary["has_cadence"] is True
    assert not strava.uploads

    run(svc.poll_once())  # seen already: no second prompt
    assert len(notifier.prompts) == 1

    result = run(svc.apply_fix(2, 10, 180))
    assert svc.store.get(2).status == st.UPLOADED
    assert svc.store.get(2).strava_activity_id == result.activity_id
    data, kw = strava.uploads[0]
    assert kw["trainer"] is True and kw["data_type"] == "fit"
    assert strava.gear == (result.activity_id, {"gear_id": "b123"})

    session = Decoder(Stream.from_byte_array(bytearray(data))).read()[0]["session_mesgs"][0]
    assert session["total_distance"] == pytest.approx(10 * 1609.344, abs=1)
    assert session["avg_power"] == 180
    assert "10 mi @ 180 W" in notifier.resolved[0]

    with pytest.raises(ServiceError):  # can't fix twice
        run(svc.apply_fix(2, 10, None))


def test_undelivered_prompt_is_retried(env):
    svc, garmin, strava, notifier = env
    run(svc.poll_once())
    garmin.activities = [act(8, "indoor_cycling", "2026-10-30 11:00:00")]
    real_prompt = notifier.prompt_ride

    async def fail(activity):
        notifier.prompts.append(activity)
        return None

    notifier.prompt_ride = fail
    run(svc.poll_once())
    assert svc.store.get(8).discord_message_id is None
    notifier.prompt_ride = real_prompt
    run(svc.poll_once())
    assert svc.store.get(8).discord_message_id == 555
    assert len(notifier.prompts) == 2
    run(svc.poll_once())  # delivered now: no more prompts
    assert len(notifier.prompts) == 2


def test_other_activities_forwarded_unchanged(env):
    svc, garmin, strava, notifier = env
    run(svc.poll_once())
    garmin.activities = [act(3, "road_biking", "2026-10-30 12:00:00")]
    run(svc.poll_once())
    assert svc.store.get(3).status == st.UPLOADED
    data, kw = strava.uploads[0]
    assert data == indoor_ride() and kw.get("trainer") is None
    assert not notifier.prompts


def test_duplicate_forward_warns_once(env):
    svc, garmin, strava, notifier = env
    run(svc.poll_once())
    strava.duplicate = True
    garmin.activities = [act(4, "running", "2026-10-30 07:00:00"), act(5, "running", "2026-10-30 08:00:00")]
    run(svc.poll_once())
    assert svc.store.get(4).status == st.DUPLICATE
    assert sum("sync is probably still on" in m for m in notifier.messages) == 1


def test_duplicate_indoor_can_be_retried(env):
    svc, garmin, strava, notifier = env
    run(svc.poll_once())
    garmin.activities = [act(6, "indoor_cycling", "2026-10-30 11:00:00")]
    run(svc.poll_once())
    strava.duplicate = True
    with pytest.raises(ServiceError, match="already has"):
        run(svc.apply_fix(6, 12, None))
    strava.duplicate = False
    run(svc.apply_fix(6, 12, None))
    assert svc.store.get(6).status == st.UPLOADED


def test_failed_upload_returns_to_pending(env):
    svc, garmin, strava, notifier = env
    run(svc.poll_once())
    garmin.activities = [act(7, "indoor_cycling", "2026-10-30 11:00:00")]
    run(svc.poll_once())

    def boom(*a, **k):
        from indoor_rides.strava import StravaError
        raise StravaError("Strava is down")

    strava.upload = boom
    with pytest.raises(ServiceError, match="Strava is down"):
        run(svc.apply_fix(7, 12, None))
    assert svc.store.get(7).status == st.PENDING


def test_poll_errors_reported_once_then_recovery(env):
    svc, garmin, strava, notifier = env

    def fail():
        raise RuntimeError("network down")

    garmin.recent_activities = fail
    run(svc.poll_once())
    run(svc.poll_once())
    assert sum("network down" in m for m in notifier.messages) == 1
    garmin.recent_activities = lambda: []
    run(svc.poll_once())
    assert "working again" in notifier.messages[-1]


def test_key_expiry_warning(env):
    from datetime import date, timedelta
    svc, garmin, strava, notifier = env
    svc.cfg = replace(svc.cfg, garmin_key_expires=date.today() + timedelta(days=3))
    run(svc.poll_once())
    run(svc.poll_once())
    assert sum("expires in 3 day" in m for m in notifier.messages) == 1


@pytest.mark.parametrize("text,expected", [
    ("12.4", (12.4, None)),
    ("12.4 mi", (12.4, None)),
    ("12.4 185", (12.4, 185.0)),
    ("12.4, 185w", (12.4, 185.0)),
    ("20km @ 190 watts", (20.0, 190.0)),
    ("hello", None),
    ("0", None),
])
def test_parse_reply(text, expected):
    assert parse_reply(text) == expected
