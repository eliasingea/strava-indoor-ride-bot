"""Discord front end: DMs you about new indoor rides and takes your answer.

Each prompt has three buttons:
  * Enter distance — opens a form for distance and optional average watts
  * Upload as-is   — sends the original file to Strava unchanged
  * Don't upload   — skips the ride

You can also just reply to the prompt with text like ``12.4`` or ``12.4 185w``.
Buttons keep working across restarts (their IDs encode the Garmin activity).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

import discord
from discord.ext import tasks

from . import store as st
from .config import Config
from .service import Service, ServiceError

log = logging.getLogger(__name__)

_REPLY_RE = re.compile(
    r"^\s*(?P<dist>\d+(?:\.\d+)?)\s*(?:mi|miles?|km)?\s*"
    r"(?:[,/@ ]\s*(?P<watts>\d+(?:\.\d+)?)\s*(?:w|watts?)?)?\s*$",
    re.I,
)


def parse_reply(text: str) -> tuple[float, float | None] | None:
    """Parse '12.4', '12.4 mi', '12.4 185', '12.4, 185w', '20km @ 190w'."""
    m = _REPLY_RE.match(text)
    if not m:
        return None
    dist = float(m["dist"])
    watts = float(m["watts"]) if m["watts"] else None
    if dist <= 0 or (watts is not None and watts <= 0):
        return None
    return dist, watts


def format_prompt(activity: st.Activity, units: str) -> str:
    s = activity.summary
    parts = [_when(s.get("start_local") or s.get("start"))]
    if s.get("duration_s"):
        parts.append(_duration(s["duration_s"]))
    if s.get("avg_hr"):
        parts.append(f"avg HR {s['avg_hr']:.0f}")
    if s.get("avg_cadence"):
        parts.append(f"{s['avg_cadence']:.0f} rpm")
    if s.get("has_power") and s.get("avg_power"):
        parts.append(f"{s['avg_power']:.0f} W recorded")
    lines = [
        f"🚴 **{activity.name or 'Indoor ride'}**",
        " · ".join(p for p in parts if p),
        f"How far did you ride? Tap **Enter distance**, or reply with e.g. `12.4` or `12.4 185w` ({units}).",
    ]
    if not s.get("has_cadence"):
        lines.append("_No cadence in this file, so distance will be spread evenly over time._")
    return "\n".join(lines)


class RideButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ride:(?P<action>fix|asis|ignore):(?P<gid>\d+)",
):
    STYLES = {
        "fix": ("Enter distance", discord.ButtonStyle.primary),
        "asis": ("Upload as-is", discord.ButtonStyle.secondary),
        "ignore": ("Don't upload", discord.ButtonStyle.danger),
    }

    def __init__(self, action: str, garmin_id: int):
        label, style = self.STYLES[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"ride:{action}:{garmin_id}"))
        self.action = action
        self.garmin_id = garmin_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # noqa: ARG003 - discord.py signature
        return cls(match["action"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: RideBot = interaction.client  # type: ignore[assignment]
        if interaction.user.id != bot.cfg.discord_user_id:
            await interaction.response.send_message("This isn't your ride.", ephemeral=True)
            return
        if self.action == "fix":
            await interaction.response.send_modal(DistanceModal(bot, self.garmin_id))
            return
        await interaction.response.defer(thinking=True)
        try:
            if self.action == "asis":
                result = await bot.service.upload_as_is(self.garmin_id)
                await interaction.followup.send(f"Uploaded unchanged: {result.url}")
            else:
                await bot.service.ignore(self.garmin_id)
                await interaction.followup.send("OK, not uploading that one.")
        except ServiceError as exc:
            await interaction.followup.send(f"⚠️ {exc}")


class DistanceModal(discord.ui.Modal):
    def __init__(self, bot: RideBot, garmin_id: int):
        super().__init__(title="Fix indoor ride", timeout=None)
        self.bot = bot
        self.garmin_id = garmin_id
        self.distance = discord.ui.TextInput(placeholder="12.4", max_length=8)
        self.watts = discord.ui.TextInput(placeholder="185", required=False, max_length=5)
        self.add_item(discord.ui.Label(text=f"Distance ({bot.cfg.units})", component=self.distance))
        self.add_item(discord.ui.Label(
            text="Average watts (optional)", component=self.watts,
            description="Leave blank to keep the file's power (if any)",
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            distance = float(self.distance.value.strip())
            watts = float(self.watts.value.strip()) if self.watts.value.strip() else None
            if distance <= 0 or (watts is not None and watts <= 0):
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "Couldn't read that. Use numbers like `12.4` and `185`.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        await self.bot.fix_and_reply(self.garmin_id, distance, watts, interaction.followup.send)


class RideBot(discord.Client):
    def __init__(self, cfg: Config):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.dm_messages = True  # DM content needs no privileged intent
        super().__init__(intents=intents)
        self.cfg = cfg
        self.service: Service  # set by attach()
        self._owner: discord.User | None = None

    def attach(self, service: Service) -> None:
        self.service = service

    async def setup_hook(self) -> None:
        self.add_dynamic_items(RideButton)
        self.poll_loop.change_interval(minutes=self.cfg.poll_minutes)
        self.poll_loop.start()

    async def on_ready(self) -> None:
        log.info("Discord connected as %s", self.user)
        if not getattr(self, "_announced", False):  # on_ready also fires on reconnects
            self._announced = True
            pending = len(self.service.pending())
            extra = f" {pending} ride(s) waiting for a distance; send `status`." if pending else ""
            await self.notify(f"🟢 Online. Checking Garmin every {self.cfg.poll_minutes:g} min.{extra}")

    @tasks.loop(minutes=5)
    async def poll_loop(self) -> None:
        await self.service.poll_once()

    @poll_loop.before_loop
    async def _before_poll(self) -> None:
        await self.wait_until_ready()

    # Notifier ----------------------------------------------------------- #

    async def owner(self) -> discord.User:
        if self._owner is None:
            self._owner = await self.fetch_user(self.cfg.discord_user_id)
        return self._owner

    async def notify(self, text: str) -> None:
        try:
            await (await self.owner()).send(text)
        except discord.HTTPException:
            log.exception("Couldn't DM: %s", text)

    async def prompt_ride(self, activity: st.Activity) -> int | None:
        view = discord.ui.View(timeout=None)
        for action in ("fix", "asis", "ignore"):
            view.add_item(RideButton(action, activity.garmin_id))
        try:
            msg = await (await self.owner()).send(format_prompt(activity, self.cfg.units), view=view)
        except discord.HTTPException:
            log.exception("Couldn't DM ride prompt for %s", activity.garmin_id)
            return None
        return msg.id

    async def resolve_prompt(self, activity: st.Activity, status_line: str) -> None:
        if not activity.discord_message_id:
            return
        try:
            channel = await (await self.owner()).create_dm()
            msg = await channel.fetch_message(activity.discord_message_id)
            await msg.edit(content=f"{msg.content}\n\n{status_line}", view=None)
        except discord.HTTPException:
            log.warning("Couldn't update prompt for %s", activity.garmin_id)

    # Text replies -------------------------------------------------------- #

    async def on_message(self, message: discord.Message) -> None:
        if (
            message.author.bot
            or message.author.id != self.cfg.discord_user_id
            or message.guild is not None
        ):
            return
        text = message.content.strip()

        if text.lower() in ("status", "pending", "list"):
            pending = self.service.pending()
            if not pending:
                await message.reply("Nothing waiting. 👍")
            else:
                lines = [f"• {a.name} ({_when(a.summary.get('start_local'))})" for a in pending]
                await message.reply("Waiting for distance:\n" + "\n".join(lines))
            return

        parsed = parse_reply(text)
        if parsed is None:
            await message.reply(
                "Reply to a ride with a distance like `12.4` or `12.4 185w`, or send `status`."
            )
            return

        target = await self._target_for(message)
        if target is None:
            return
        await self.fix_and_reply(target.garmin_id, *parsed, message.reply)

    async def _target_for(self, message: discord.Message) -> st.Activity | None:
        if message.reference and message.reference.message_id:
            activity = self.service.store.by_discord_message(message.reference.message_id)
            if activity:
                return activity
        pending = self.service.pending()
        if len(pending) == 1:
            return pending[0]
        if not pending:
            await message.reply("No rides are waiting for a distance.")
        else:
            await message.reply(f"{len(pending)} rides are waiting. Reply directly to the one you mean.")
        return None

    async def fix_and_reply(self, garmin_id: int, distance: float, watts: float | None, send) -> None:
        try:
            result = await self.service.apply_fix(garmin_id, distance, watts)
        except ServiceError as exc:
            await send(f"⚠️ {exc}")
            return
        await send(f"✅ On Strava: {result.url}")


def _when(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%a %b %-d, %-I:%M %p")


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
