# strava-indoor-rides

Fixes the distance on Garmin indoor rides before they reach Strava.

```
Garmin watch/computer ──► Garmin Connect
                              │  (polled every 5 min via the official Personal API)
                              ▼
                     indoor-rides (Raspberry Pi)
                       │                     │
        indoor ride?   │ yes                 │ no
                       ▼                     ▼
   Discord DM: "How far?"            upload original to Strava
   you: "12.4 185w"
                       │
   rewrite FIT (distance, speed, optional power) ──► upload to Strava
```

Strava's API can't change an activity's distance or delete one, so the fix has to happen
**before** Strava sees the ride. That's why Garmin's own Strava sync gets turned off and this
service does the uploading, for indoor rides (fixed) and everything else (unchanged).

## How the rewrite works

The original FIT file is decoded and re-encoded with Garmin's official FIT SDK. Heart rate,
cadence, laps, device and timestamps are kept. The only changes:

- **Distance/speed**: your total is spread across the ride in proportion to cadence, so coasting
  adds no distance and speed follows your pedalling. With no cadence it's spread evenly over time.
  Lap and session totals are recalculated to match.
- **Power** (only if you enter watts): `POWER_MODE=constant` writes the same wattage every second.
  `cadence` scales it by cadence (0 W when coasting). Both average to the number you entered.
  Strava will treat this as power-meter data, so it counts toward your power curve and best efforts.
  If the file already has power and you leave watts blank, the recorded power is kept.

## Setup

### 1. Garmin Personal API key
Go to <https://connect.garmin.com/app/settings/personalApiToken>, create a key (pick **1 year**),
and copy it. It's shown only once. Put the expiry date in `GARMIN_KEY_EXPIRES` and the bot will
remind you two weeks before it runs out.

### 2. Strava API app
Go to <https://www.strava.com/settings/api> and create an app. Name and website can be anything.
Set **Authorization Callback Domain** to `localhost`. Copy the Client ID and Client Secret.

### 3. Discord bot
1. Go to <https://discord.com/developers/applications> → **New Application**.
2. Open **Bot** → **Reset Token** → copy the token. No privileged intents are needed.
3. Open **OAuth2 → URL Generator**, tick scope `bot`, open the generated URL, and add the bot to a
   server you're in. A private server with just you works. Discord only lets bots DM people they
   share a server with.
4. In Discord, enable **Settings → Advanced → Developer Mode**, then right-click your name →
   **Copy User ID**.

### 4. Install

The Garmin client needs **Python 3.12+**. Raspberry Pi OS based on Debian 13 (Trixie) ships 3.13.
On older Bookworm images, install [uv](https://docs.astral.sh/uv/) and use `uv venv --python 3.12`.

```sh
git clone <this repo> ~/strava-indoor-rides && cd ~/strava-indoor-rides
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env && chmod 600 .env   # fill it in
.venv/bin/indoor-rides strava-auth       # one-time; follow the prompts
.venv/bin/indoor-rides garmin-list       # check your indoor rides show as ASK
```

If your indoor rides show a different type than `indoor_cycling`, add it to
`INDOOR_ACTIVITY_TYPES`.

### 5. Try it on a real file first (optional)
Download an indoor ride from Garmin Connect (⚙ → Export Original), then:

```sh
.venv/bin/indoor-rides fix ~/Downloads/123456789_ACTIVITY.zip --distance 12.4 --watts 185
```

You can upload the resulting `-fixed.fit` to Strava by hand (strava.com → Upload) to see how it looks.

### 6. Switch over
1. **Turn off Garmin's Strava sync**: Garmin Connect → Settings → Connected Apps → Strava →
   Disconnect. If you skip this, Strava gets the unfixed ride first and the fixed upload is rejected
   as a duplicate. The bot will tell you if it sees this.
2. Start the bot: `.venv/bin/indoor-rides run`. On the first poll it records your existing
   activities and leaves them alone. Only new ones are handled.

### 7. Run it on the Pi permanently
```sh
sudo cp deploy/indoor-rides.service /etc/systemd/system/
# edit User= and paths in the unit if you're not user `pi` in ~/strava-indoor-rides
sudo systemctl enable --now indoor-rides
journalctl -u indoor-rides -f     # logs
```

## Using it

After an indoor ride syncs to Garmin Connect, you'll get a DM within ~5 minutes:

> 🚴 **Indoor Cycling**
> Sat Oct 31, 7:02 AM · 45:12 · avg HR 142 · 87 rpm
> [Enter distance] [Upload as-is] [Don't upload]

- **Enter distance** opens a form for distance and optional average watts.
- Or just **reply** to the message: `12.4`, `12.4 185w`, `20km @ 190`.
- Send `status` to list rides still waiting.

Rides wait as long as you like. Buttons keep working after restarts. If an upload fails, the
ride goes back to waiting and you can try again.

## Development

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

| File | What it does |
| --- | --- |
| `src/indoor_rides/fitfix.py` | FIT decode → rewrite distance/power → encode |
| `src/indoor_rides/service.py` | Poll Garmin, hold indoor rides, upload to Strava |
| `src/indoor_rides/bot.py` | Discord DMs, buttons, form, text replies |
| `src/indoor_rides/garmin.py` | Garmin Personal API (official client) |
| `src/indoor_rides/strava.py` | Strava OAuth, uploads, gear updates |
| `src/indoor_rides/store.py` | SQLite state in `data/state.db` |
