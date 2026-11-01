"""Command line entry point.

    indoor-rides run            start the bot (default)
    indoor-rides strava-auth    one-time Strava authorization
    indoor-rides garmin-list    show recent Garmin activities and how they'd be handled
    indoor-rides fix FILE ...   rewrite a local FIT file (for testing)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config as config_mod
from . import fitfix, strava


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="indoor-rides")
    parser.add_argument("--env", help="path to .env file (default: ./.env)")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="start the bot")
    sub.add_parser("strava-auth", help="authorize with Strava (one time)")
    sub.add_parser("garmin-list", help="list recent Garmin activities")
    fix = sub.add_parser("fix", help="rewrite a local FIT file")
    fix.add_argument("file", type=Path, help="FIT file or Garmin download ZIP")
    fix.add_argument("--distance", type=float, required=True, help="in DISTANCE_UNITS")
    fix.add_argument("--watts", type=float)
    fix.add_argument("-o", "--output", type=Path)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("discord").setLevel(logging.WARNING)

    try:
        cfg = config_mod.load(args.env)
        return {
            None: cmd_run, "run": cmd_run, "strava-auth": cmd_strava_auth,
            "garmin-list": cmd_garmin_list, "fix": cmd_fix,
        }[args.cmd](cfg, args)
    except (config_mod.ConfigError, strava.StravaError, fitfix.FitError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def cmd_run(cfg: config_mod.Config, _args) -> int:
    from .bot import RideBot
    from .garmin import Garmin
    from .service import Service
    from .store import Store

    cfg.require("garmin_api_key", "strava_client_id", "strava_client_secret",
                "discord_bot_token", "discord_user_id")
    if not cfg.strava_tokens_path.exists():
        raise config_mod.ConfigError("Not authorized with Strava yet. Run: indoor-rides strava-auth")

    bot = RideBot(cfg)
    service = Service(
        cfg,
        Store(cfg.db_path),
        Garmin(cfg.garmin_api_key, cfg.garmin_token_cache),
        strava.Strava(cfg.strava_client_id, cfg.strava_client_secret, cfg.strava_tokens_path),
        notifier=bot,
    )
    bot.attach(service)
    bot.run(cfg.discord_bot_token, log_handler=None)
    return 0


def cmd_strava_auth(cfg: config_mod.Config, _args) -> int:
    cfg.require("strava_client_id", "strava_client_secret")
    print("1. Open this URL and click Authorize:\n")
    print("   " + strava.authorize_url(cfg.strava_client_id) + "\n")
    print("2. Your browser will land on a page that fails to load (http://localhost/...).")
    print("   That's expected. Copy the whole URL from the address bar and paste it here.\n")
    pasted = input("URL: ").strip()
    query = parse_qs(urlparse(pasted).query)
    if "error" in query:
        raise strava.StravaError(f"Strava returned an error: {query['error'][0]}")
    code = query.get("code", [pasted])[0]
    scope = query.get("scope", [""])[0]
    if scope and "activity:write" not in scope:
        raise strava.StravaError("The 'upload activities' permission wasn't granted. Run again and leave it ticked.")

    strava.exchange_code(cfg.strava_client_id, cfg.strava_client_secret, code, cfg.strava_tokens_path)
    client = strava.Strava(cfg.strava_client_id, cfg.strava_client_secret, cfg.strava_tokens_path)
    athlete = client.athlete()
    print(f"\nAuthorized as {athlete.get('firstname')} {athlete.get('lastname')}. "
          f"Tokens saved to {cfg.strava_tokens_path}")
    bikes = athlete.get("bikes") or []
    if bikes:
        print("\nYour bikes (set STRAVA_INDOOR_GEAR_ID to auto-assign one to indoor rides):")
        for b in bikes:
            print(f"   {b['id']}  {b.get('name')}")
    return 0


def cmd_garmin_list(cfg: config_mod.Config, _args) -> int:
    from .garmin import Garmin

    cfg.require("garmin_api_key")
    activities = Garmin(cfg.garmin_api_key, cfg.garmin_token_cache).recent_activities()
    print(f"{'ID':>12}  {'Start (local)':19}  {'Type':22}  {'Handling':10}  Name")
    for a in activities:
        if a.type_key in cfg.indoor_types:
            handling = "ASK"
        elif cfg.forward_other_activities:
            handling = "forward"
        else:
            handling = "skip"
        start = str(a.raw.get("startTimeLocal") or a.start_gmt or "")[:19]
        print(f"{a.id:>12}  {start:19}  {a.type_key or '?':22}  {handling:10}  {a.name or ''}")
    print(f"\nIndoor types (INDOOR_ACTIVITY_TYPES): {', '.join(sorted(cfg.indoor_types))}")
    return 0


def cmd_fix(cfg: config_mod.Config, args) -> int:
    _, data = fitfix.extract_activity_file(args.file.read_bytes())
    before = fitfix.summarize(data)
    fixed = fitfix.fix_ride(data, args.distance * cfg.unit_m, args.watts, cfg.power_mode)
    after = fitfix.summarize(fixed)
    out = args.output or args.file.with_name(args.file.stem + "-fixed.fit")
    out.write_bytes(fixed)
    print(f"Distance: {before.distance_m / cfg.unit_m:.2f} -> {after.distance_m / cfg.unit_m:.2f} {cfg.units}")
    if args.watts:
        print(f"Avg power: {before.avg_power or 0:.0f} -> {after.avg_power:.0f} W ({cfg.power_mode})")
    print(f"Cadence in file: {'yes' if before.has_cadence else 'no'}")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
