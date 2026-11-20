"""Giveaways: setup page, /giveaway create/manage, entry/reroll buttons and the tick loop."""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import random
import re
import uuid

import discord
from discord import app_commands

import permissions
from config import config, save

GOLD = 0xEFCC62
ENTER_PREFIX = "giveaway_enter:"
REROLL_PREFIX = "giveaway_reroll:"
DONE_PREFIX = "giveaway_done:"
_DURATION_RE = re.compile(r"^(\d+)\s*([smhd])$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_VALUE_RE = re.compile(r"^(\d+)\s*p?$")

# Entries live in memory until the tick loop persists them: a giveaway can take
# hundreds of entries in a burst without a config write or embed edit per click.
_entry_cache: dict[str, set[int]] = {}
_dirty: set[str] = set()

# Discord allows five message edits per five seconds per channel. Giveaways often
# share one channel, so space the tick's embed edits out rather than eating 429s.
_EDIT_SPACING = 1.1


# --------------------------------------------------------------------------- helpers


def parse_duration(text: str) -> dt.timedelta | None:
    """Parse a duration like '30s', '30m', '2h' or '7d'."""
    match = _DURATION_RE.fullmatch(text.strip().lower())
    if match is None:
        return None
    return dt.timedelta(seconds=int(match.group(1)) * _DURATION_UNITS[match.group(2)])


def parse_value(text: str) -> int | None:
    """Parse a host value like '150' or '150p'; the p is normalized away."""
    match = _VALUE_RE.fullmatch(text.strip().lower())
    if match is None:
        return None
    value = int(match.group(1))
    return value if value > 0 else None


def points_for(user_id: int) -> int:
    """Points from giveaways this user hosted that were marked done and not revoked."""
    return sum(
        giveaway.get("value", 0)
        for giveaway in config["giveaways"].values()
        if giveaway.get("host_id") == user_id
        and giveaway.get("status") == "done"
        and not giveaway.get("points_revoked")
    )


async def apply_host_roles(guild: discord.Guild, user_id: int) -> bool:
    """Give a host the highest tier their points qualify for. True when nothing failed."""
    tiers = config["giveaway_role_tiers"]
    if not all(tier.get("role_id") and tier.get("points") is not None for tier in tiers):
        return True
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            return True
    points = points_for(user_id)
    roles = [guild.get_role(tier["role_id"]) for tier in tiers]
    target = None
    for role, tier in zip(roles, tiers):
        if role is not None and points >= tier["points"]:
            target = role
    try:
        for role in roles:
            if role is None:
                continue
            if role is target:
                if role not in member.roles:
                    await member.add_roles(role, reason="Giveaway host points")
            elif role in member.roles:
                await member.remove_roles(role, reason="Giveaway host points")
    except discord.HTTPException:
        return False
    return True


async def apply_all_host_roles(guild: discord.Guild) -> int:
    """Re-check every giveaway host after the tiers change. Returns the failure count."""
    failures = 0
    host_ids = {
        giveaway.get("host_id")
        for giveaway in config["giveaways"].values()
        if giveaway.get("host_id")
    }
    for user_id in host_ids:
        if not await apply_host_roles(guild, user_id):
            failures += 1
    return failures


def is_manager(user: discord.abc.User) -> bool:
    """Administrators or the giveaway manager role from setup."""
    if not isinstance(user, discord.Member):
        return False
    if permissions.is_admin(user):
        return True
    role_id = config["giveaway_manager_role_id"]
    return bool(role_id) and any(role.id == role_id for role in user.roles)


def _entries(giveaway_id: str) -> set[int]:
    entries = _entry_cache.get(giveaway_id)
    if entries is None:
        giveaway = config["giveaways"].get(giveaway_id, {})
        entries = set(giveaway.get("entries", []))
        _entry_cache[giveaway_id] = entries
    return entries


def build_embed(giveaway: dict) -> discord.Embed:
    ended = giveaway.get("status") in ("ended", "done")
    embed = discord.Embed(
        title=giveaway["title"][:256],
        color=discord.Color.red() if ended else discord.Color(GOLD),
    )
    embed.add_field(name="Host", value=f"<@{giveaway['host_id']}>", inline=True)
    if ended:
        winners = (
            ", ".join(f"<@{uid}>" for uid in giveaway.get("winners", [])) or "No entries"
        )[:1024]
    else:
        winners = str(giveaway["winner_count"])
    embed.add_field(name="Winners", value=winners, inline=True)
    embed.add_field(name="Entries", value=str(len(giveaway.get("entries", []))), inline=True)
    ends = int(dt.datetime.fromisoformat(giveaway["ends_at"]).timestamp())
    embed.add_field(name="Ends", value=f"<t:{ends}:R>", inline=True)
    image = giveaway.get("image") or giveaway.get("thumbnail")
    if image:
        embed.set_image(url=image)
    return embed


def _is_due(giveaway: dict, now: dt.datetime) -> bool:
    return now >= dt.datetime.fromisoformat(giveaway["ends_at"])


async def _edit_embed(bot: discord.Client, giveaway: dict, view=discord.utils.MISSING) -> None:
    channel = bot.get_channel(giveaway["channel_id"]) if giveaway.get("channel_id") else None
    if channel is None or not giveaway.get("message_id"):
        return
    try:
        message = await channel.fetch_message(giveaway["message_id"])
        if view is discord.utils.MISSING:
            await message.edit(embed=build_embed(giveaway))
        else:
            await message.edit(embed=build_embed(giveaway), view=view)
    except discord.HTTPException:
        pass


def build_log_embed(giveaway: dict) -> discord.Embed:
    """The giveaway log entry, rebuilt from the record so it can be edited later."""
    jump_url = giveaway.get("jump_url")
    ends = int(dt.datetime.fromisoformat(giveaway["ends_at"]).timestamp())
    embed = discord.Embed(
        title="Giveaway created",
        color=discord.Color(GOLD),
        url=jump_url,
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    if jump_url:
        embed.description = f"[Jump to giveaway]({jump_url})"
    embed.add_field(name="Title", value=giveaway["title"][:1024], inline=False)
    embed.add_field(name="Host", value=f"<@{giveaway['host_id']}>", inline=True)
    embed.add_field(name="Value", value=str(giveaway["value"]), inline=True)
    embed.add_field(name="Winners", value=str(giveaway["winner_count"]), inline=True)
    embed.add_field(name="Channel", value=f"<#{giveaway['channel_id']}>", inline=True)
    embed.add_field(name="Ends", value=f"<t:{ends}:R>", inline=True)
    embed.add_field(name="Created by", value=f"<@{giveaway['created_by_id']}>", inline=True)
    if giveaway.get("ended_by_id"):
        embed.add_field(name="Ended early by", value=f"<@{giveaway['ended_by_id']}>", inline=True)
    if giveaway.get("done_by_id"):
        embed.add_field(name="Marked done by", value=f"<@{giveaway['done_by_id']}>", inline=True)
    image = giveaway.get("image") or giveaway.get("thumbnail")
    if image:
        embed.set_image(url=image)
    return embed


async def log_giveaway(
    bot: discord.Client, giveaway: dict, file: discord.File | None = None
) -> discord.Message | None:
    """Post the creation record to the configured giveaway log channel."""
    channel = (
        bot.get_channel(config["giveaway_log_channel_id"])
        if config["giveaway_log_channel_id"]
        else None
    )
    if channel is None:
        return None
    try:
        return await channel.send(embed=build_log_embed(giveaway), file=file)
    except discord.HTTPException:
        return None


async def _refresh_log(bot: discord.Client, giveaway: dict) -> None:
    """Re-render the log entry after a manual end or a mark-done."""
    channel = (
        bot.get_channel(giveaway["log_channel_id"]) if giveaway.get("log_channel_id") else None
    )
    if channel is None or not giveaway.get("log_message_id"):
        return
    try:
        message = await channel.fetch_message(giveaway["log_message_id"])
        await message.edit(embed=build_log_embed(giveaway))
    except discord.HTTPException:
        pass


async def _dm_winners(bot: discord.Client, giveaway: dict) -> None:
    for user_id in giveaway.get("winners", []):
        try:
            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            await user.send(f"🎉 You won **{giveaway['title']}**!")
        except discord.HTTPException:
            pass


async def end_giveaway(
    bot: discord.Client,
    giveaway_id: str,
    giveaway: dict,
    ended_by: discord.abc.User | None = None,
) -> list[int]:
    """Draw the winners, turn the embed red and DM them. Returns the winner ids."""
    entries = sorted(_entries(giveaway_id))
    winners = random.sample(entries, min(giveaway["winner_count"], len(entries)))
    giveaway.update(
        {
            "status": "ended",
            "entries": entries,
            "winners": winners,
            "past_winners": list(winners),
            "ended_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
    )
    if ended_by is not None:
        giveaway["ended_by_id"] = ended_by.id
    _dirty.discard(giveaway_id)
    save()
    view = GiveawayRerollView(giveaway_id)
    await _edit_embed(bot, giveaway, view)
    bot.add_view(view)
    if ended_by is not None:
        await _refresh_log(bot, giveaway)
    await _dm_winners(bot, giveaway)
    return winners


async def mark_done(
    bot: discord.Client,
    guild: discord.Guild,
    giveaway_id: str,
    giveaway: dict,
    done_by: discord.abc.User,
) -> bool:
    """Mark a decided giveaway done, award its points and refresh the host's roles."""
    giveaway["status"] = "done"
    giveaway["done_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    giveaway["done_by_id"] = done_by.id
    save()
    view = GiveawayRerollView(giveaway_id, done=True)
    await _edit_embed(bot, giveaway, view)
    bot.add_view(view)
    await _refresh_log(bot, giveaway)
    return await apply_host_roles(guild, giveaway["host_id"])


async def tick(bot: discord.Client) -> None:
    """Persist new entries, refresh their counts and end giveaways that are due."""
    now = dt.datetime.now(dt.timezone.utc)
    changed = False
    last_edit: dict[int, float] = {}

    async def pace(channel_id: int) -> None:
        # Several giveaways can be dirty in the same channel in one tick; keep
        # consecutive edits there at least _EDIT_SPACING apart.
        loop = asyncio.get_running_loop()
        last = last_edit.get(channel_id)
        if last is not None:
            wait = _EDIT_SPACING - (loop.time() - last)
            if wait > 0:
                await asyncio.sleep(wait)
        last_edit[channel_id] = loop.time()

    for giveaway_id, giveaway in list(config["giveaways"].items()):
        if giveaway.get("status") == "active" and _is_due(giveaway, now):
            if giveaway.get("channel_id") and giveaway.get("message_id"):
                await pace(giveaway["channel_id"])
            await end_giveaway(bot, giveaway_id, giveaway)
            changed = True
            continue
        if giveaway_id in _dirty:
            giveaway["entries"] = sorted(_entries(giveaway_id))
            _dirty.discard(giveaway_id)
            changed = True
            if giveaway.get("channel_id") and giveaway.get("message_id"):
                await pace(giveaway["channel_id"])
            await _edit_embed(bot, giveaway)
    if changed:
        save()


# --------------------------------------------------------------------------- panels


class GiveawayEntryView(discord.ui.View):
    """The Enter button under a running giveaway."""

    def __init__(self, giveaway_id: str):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id
        button = discord.ui.Button(
            label="Enter giveaway",
            emoji="🎉",
            style=discord.ButtonStyle.success,
            custom_id=f"{ENTER_PREFIX}{giveaway_id}",
        )
        button.callback = self._enter
        self.add_item(button)

    async def _enter(self, interaction: discord.Interaction) -> None:
        giveaway = config["giveaways"].get(self.giveaway_id)
        if giveaway is None or giveaway.get("status") != "active":
            await interaction.response.send_message(
                "This giveaway has ended.", ephemeral=True
            )
            return
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            await interaction.response.send_message(
                "Enter from inside the server.", ephemeral=True
            )
            return
        entries = _entries(self.giveaway_id)
        if interaction.user.id in entries:
            # Already in — offer to leave, but only after an explicit confirmation.
            await interaction.response.send_message(
                "You're entered. Press **Leave giveaway** to give your slot back.",
                view=GiveawayLeaveView(self.giveaway_id),
                ephemeral=True,
            )
            return
        blacklist_id = config["giveaway_blacklist_role_id"]
        if blacklist_id and any(role.id == blacklist_id for role in interaction.user.roles):
            await interaction.response.send_message(
                "You can't enter giveaways with your current roles.", ephemeral=True
            )
            return
        entries.add(interaction.user.id)
        _dirty.add(self.giveaway_id)
        await interaction.response.send_message(
            f"✅ You're entered — {len(entries)} entries so far.", ephemeral=True
        )


class GiveawayLeaveView(discord.ui.View):
    """Ephemeral confirm shown when an entrant presses the Enter button again."""

    def __init__(self, giveaway_id: str):
        super().__init__(timeout=60)
        self.giveaway_id = giveaway_id

    @discord.ui.button(label="Leave giveaway", style=discord.ButtonStyle.danger, emoji="👋")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        giveaway = config["giveaways"].get(self.giveaway_id)
        if giveaway is None or giveaway.get("status") != "active":
            await interaction.response.edit_message(
                content="This giveaway has ended.", view=None
            )
            return
        entries = _entries(self.giveaway_id)
        entries.discard(interaction.user.id)
        _dirty.add(self.giveaway_id)
        await interaction.response.edit_message(
            content=f"You've left the giveaway — {len(entries)} entries so far.",
            view=None,
        )


class GiveawayRerollView(discord.ui.View):
    """The Reroll and Mark as done buttons under a decided giveaway."""

    def __init__(self, giveaway_id: str, done: bool = False):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id
        reroll = discord.ui.Button(
            label="Reroll winners",
            emoji="🎲",
            style=discord.ButtonStyle.blurple,
            custom_id=f"{REROLL_PREFIX}{giveaway_id}",
        )
        reroll.callback = self._reroll
        self.add_item(reroll)
        finish = discord.ui.Button(
            label="Mark as done",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=f"{DONE_PREFIX}{giveaway_id}",
            disabled=done,
        )
        finish.callback = self._mark_done
        self.add_item(finish)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_manager(interaction.user):
            return True
        await interaction.response.send_message(
            "Only giveaway managers can use these buttons.", ephemeral=True
        )
        return False

    async def _mark_done(self, interaction: discord.Interaction) -> None:
        giveaway = config["giveaways"].get(self.giveaway_id)
        if giveaway is None or giveaway.get("status") not in ("ended", "done"):
            await interaction.response.send_message(
                "This giveaway is still running.", ephemeral=True
            )
            return
        if giveaway.get("status") == "done":
            await interaction.response.send_message(
                "This giveaway is already marked done.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        roles_ok = await mark_done(
            interaction.client,
            interaction.guild,
            self.giveaway_id,
            giveaway,
            interaction.user,
        )
        note = "" if roles_ok else "\nCouldn't update the host's roles — check my role position."
        await interaction.followup.send(
            f"Marked **{giveaway['title']}** done — {giveaway['value']} points added "
            f"to <@{giveaway['host_id']}>.{note}",
            ephemeral=True,
        )

    async def _reroll(self, interaction: discord.Interaction) -> None:
        giveaway = config["giveaways"].get(self.giveaway_id)
        if giveaway is None or giveaway.get("status") not in ("ended", "done"):
            await interaction.response.send_message(
                "This giveaway is still running.", ephemeral=True
            )
            return
        entries = _entries(self.giveaway_id)
        past = set(giveaway.get("past_winners", []))
        eligible = sorted(entries - past)
        if not eligible:
            await interaction.response.send_message(
                "No entries left to reroll.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        winners = random.sample(eligible, min(giveaway["winner_count"], len(eligible)))
        giveaway["winners"] = winners
        giveaway["past_winners"] = sorted(past | set(winners))
        giveaway["entries"] = sorted(entries)
        save()
        try:
            await interaction.message.edit(embed=build_embed(giveaway))
        except discord.HTTPException:
            pass
        await _dm_winners(interaction.client, giveaway)
        noun = "winner" if len(winners) == 1 else "winners"
        await interaction.followup.send(
            "Rerolled — new "
            + noun
            + ": "
            + ", ".join(f"<@{uid}>" for uid in winners),
            ephemeral=True,
        )


class GiveawayManageView(discord.ui.View):
    """Ephemeral /giveaway manage page: pick an active giveaway, end it now."""

    def __init__(self):
        super().__init__(timeout=1800)
        self.selected: str | None = None
        active = [
            (giveaway_id, giveaway)
            for giveaway_id, giveaway in config["giveaways"].items()
            if giveaway.get("status") == "active"
        ]
        options = [
            discord.SelectOption(
                label=giveaway["title"][:100] or "Untitled",
                value=giveaway_id,
                description=(
                    f"{giveaway['winner_count']} winners · "
                    f"{len(giveaway.get('entries', []))} entries"
                )[:100],
            )
            for giveaway_id, giveaway in active[:25]
        ]
        select = discord.ui.Select(placeholder="Pick an active giveaway", options=options)
        select.callback = self._picked
        self.add_item(select)
        end = discord.ui.Button(label="End now", style=discord.ButtonStyle.danger, row=1)
        end.callback = self._end_now
        self.add_item(end)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_manager(interaction.user):
            return True
        await interaction.response.send_message(
            "Only giveaway managers can use this.", ephemeral=True
        )
        return False

    async def _picked(self, interaction: discord.Interaction) -> None:
        self.selected = interaction.data["values"][0]
        await interaction.response.defer()

    async def _end_now(self, interaction: discord.Interaction) -> None:
        if self.selected is None:
            await interaction.response.send_message(
                "Pick a giveaway first.", ephemeral=True
            )
            return
        giveaway = config["giveaways"].get(self.selected)
        if giveaway is None or giveaway.get("status") != "active":
            await interaction.response.send_message(
                "That giveaway has already ended.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        winners = await end_giveaway(
            interaction.client, self.selected, giveaway, interaction.user
        )
        winner_text = ", ".join(f"<@{uid}>" for uid in winners) or "no entries"
        await interaction.followup.send(
            f"Ended **{giveaway['title']}** — winners: {winner_text}", ephemeral=True
        )


# --------------------------------------------------------------------------- /setup page


class GiveawaySetupView(permissions.AdminOnlyView):
    """Giveaway setup: channel, ping role, manager role and log channel."""

    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=1800)
        self.guild = guild
        channel_default = (
            [guild.get_channel(config["giveaway_channel_id"])]
            if config["giveaway_channel_id"]
            and guild.get_channel(config["giveaway_channel_id"])
            else []
        )
        self.channel_select = discord.ui.ChannelSelect(
            placeholder="Channel that receives giveaways",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            row=0,
            default_values=channel_default,
        )
        ping_default = (
            [guild.get_role(config["giveaway_ping_role_id"])]
            if config["giveaway_ping_role_id"]
            and guild.get_role(config["giveaway_ping_role_id"])
            else []
        )
        self.ping_select = discord.ui.RoleSelect(
            placeholder="Role pinged for new giveaways",
            min_values=1,
            max_values=1,
            row=1,
            default_values=ping_default,
        )
        manager_default = (
            [guild.get_role(config["giveaway_manager_role_id"])]
            if config["giveaway_manager_role_id"]
            and guild.get_role(config["giveaway_manager_role_id"])
            else []
        )
        self.manager_select = discord.ui.RoleSelect(
            placeholder="Role that can create and manage giveaways",
            min_values=1,
            max_values=1,
            row=2,
            default_values=manager_default,
        )
        log_default = (
            [guild.get_channel(config["giveaway_log_channel_id"])]
            if config["giveaway_log_channel_id"]
            and guild.get_channel(config["giveaway_log_channel_id"])
            else []
        )
        self.log_select = discord.ui.ChannelSelect(
            placeholder="Channel that logs every new giveaway",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            row=3,
            default_values=log_default,
        )
        for select in (
            self.channel_select,
            self.ping_select,
            self.manager_select,
            self.log_select,
        ):
            select.callback = self._ack
            self.add_item(select)
        for label, style, callback in (
            ("Save setup", discord.ButtonStyle.green, self._save),
            ("Clear ping role", discord.ButtonStyle.secondary, self._clear_ping),
            ("Blacklist role →", discord.ButtonStyle.secondary, self._blacklist),
            ("← Back", discord.ButtonStyle.grey, self._back),
        ):
            button = discord.ui.Button(label=label, style=style, row=4)
            button.callback = callback
            self.add_item(button)

    async def _ack(self, interaction: discord.Interaction) -> None:
        # Selects are read on Save — just acknowledge the pick.
        await interaction.response.defer()

    async def _save(self, interaction: discord.Interaction) -> None:
        channels = self.channel_select.values or self.channel_select.default_values
        pings = self.ping_select.values or self.ping_select.default_values
        managers = self.manager_select.values or self.manager_select.default_values
        logs = self.log_select.values or self.log_select.default_values
        if not channels or not managers or not logs:
            await interaction.response.send_message(
                "Pick a channel, a manager role and a log channel first.",
                ephemeral=True,
            )
            return
        channel = self.guild.get_channel(channels[0].id)
        ping = self.guild.get_role(pings[0].id) if pings else None
        manager = self.guild.get_role(managers[0].id)
        log = self.guild.get_channel(logs[0].id)
        if channel is None or manager is None or log is None or (pings and ping is None):
            await interaction.response.send_message(
                "Something picked here no longer exists — pick it again.", ephemeral=True
            )
            return
        await interaction.response.defer()
        config["giveaway_channel_id"] = channel.id
        config["giveaway_ping_role_id"] = ping.id if ping is not None else None
        config["giveaway_manager_role_id"] = manager.id
        config["giveaway_log_channel_id"] = log.id
        save()
        ping_text = f"New giveaways ping {ping.name}." if ping else "New giveaways ping no role."
        await interaction.edit_original_response(
            content=(
                f"Saved. Giveaways post in {channel.mention}. {ping_text} "
                f"Managed by {manager.name}, logged in {log.mention}."
            ),
            view=GiveawaySetupView(self.guild),
        )

    async def _clear_ping(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        config["giveaway_ping_role_id"] = None
        save()
        await interaction.edit_original_response(
            content="Cleared. New giveaways won't ping a role.",
            view=GiveawaySetupView(self.guild),
        )

    async def _back(self, interaction: discord.Interaction) -> None:
        from views import SetupMenuView

        await interaction.response.edit_message(
            content="**Setup** — what do you want to configure?",
            view=SetupMenuView(self.guild),
        )

    async def _blacklist(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content="**Giveaways setup** — pick the role that cannot enter giveaways.",
            view=GiveawayBlacklistView(self.guild),
        )


class GiveawayBlacklistView(permissions.AdminOnlyView):
    """Giveaway setup page 2: the role whose members cannot enter giveaways."""

    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=1800)
        self.guild = guild
        default = (
            [guild.get_role(config["giveaway_blacklist_role_id"])]
            if config["giveaway_blacklist_role_id"]
            and guild.get_role(config["giveaway_blacklist_role_id"])
            else []
        )
        self.blacklist_select = discord.ui.RoleSelect(
            placeholder="Role that cannot enter giveaways",
            min_values=1,
            max_values=1,
            row=0,
            default_values=default,
        )
        self.blacklist_select.callback = self._ack
        self.add_item(self.blacklist_select)
        save_button = discord.ui.Button(
            label="Save blacklist", style=discord.ButtonStyle.green, row=1
        )
        save_button.callback = self._save
        self.add_item(save_button)
        clear_button = discord.ui.Button(
            label="Clear blacklist", style=discord.ButtonStyle.secondary, row=1
        )
        clear_button.callback = self._clear
        self.add_item(clear_button)
        back_button = discord.ui.Button(
            label="← Giveaways setup", style=discord.ButtonStyle.grey, row=1
        )
        back_button.callback = self._back
        self.add_item(back_button)

    async def _ack(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

    async def _back(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content=(
                "**Giveaways setup** — pick the giveaway channel, optional ping role, "
                "manager role and log channel, then **Save setup**."
            ),
            view=GiveawaySetupView(self.guild),
        )

    async def _save(self, interaction: discord.Interaction) -> None:
        roles = self.blacklist_select.values or self.blacklist_select.default_values
        if not roles:
            await interaction.response.send_message(
                "Pick a blacklist role first.", ephemeral=True
            )
            return
        role = self.guild.get_role(roles[0].id)
        if role is None:
            await interaction.response.send_message(
                "That role no longer exists — pick it again.", ephemeral=True
            )
            return
        await interaction.response.defer()
        config["giveaway_blacklist_role_id"] = role.id
        save()
        await interaction.edit_original_response(
            content=f"Saved. Members with {role.name} can't enter giveaways.",
            view=GiveawayBlacklistView(self.guild),
        )

    async def _clear(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        config["giveaway_blacklist_role_id"] = None
        save()
        await interaction.edit_original_response(
            content="Cleared. Everyone can enter giveaways again.",
            view=GiveawayBlacklistView(self.guild),
        )


class GiveawayRolesView(permissions.AdminOnlyView):
    """Three role tiers, each with the minimum host points needed to get it."""

    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=1800)
        self.guild = guild
        self.tiers = [dict(tier) for tier in config["giveaway_role_tiers"]]
        self.role_ids = [tier.get("role_id") for tier in self.tiers]
        self.role_selects: list[discord.ui.RoleSelect] = []
        self.points_buttons: list[discord.ui.Button] = []
        for index in range(3):
            role = guild.get_role(self.role_ids[index]) if self.role_ids[index] else None
            select = discord.ui.RoleSelect(
                placeholder=f"Tier {index + 1} role",
                min_values=1,
                max_values=1,
                row=index,
                default_values=[role] if role else [],
            )
            select.callback = self._role_callback(index)
            self.add_item(select)
            self.role_selects.append(select)
        for index in range(3):
            button = discord.ui.Button(
                label=self._points_label(index), style=discord.ButtonStyle.secondary, row=3
            )
            button.callback = self._points_callback(index)
            self.add_item(button)
            self.points_buttons.append(button)
        save_button = discord.ui.Button(
            label="Save & apply", style=discord.ButtonStyle.green, row=4
        )
        save_button.callback = self._save
        self.add_item(save_button)

    def _points_label(self, index: int) -> str:
        points = self.tiers[index].get("points")
        if points is None:
            return f"Tier {index + 1}: points not set"
        return f"Tier {index + 1}: {points} pts"

    def refresh_points_labels(self) -> None:
        for index, button in enumerate(self.points_buttons):
            button.label = self._points_label(index)

    def _role_callback(self, index: int):
        async def pick(interaction: discord.Interaction) -> None:
            self.role_ids[index] = int(interaction.data["values"][0])
            await interaction.response.defer()

        return pick

    def _points_callback(self, index: int):
        async def open_modal(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(TierPointsModal(self, index))

        return open_modal

    async def _save(self, interaction: discord.Interaction) -> None:
        points = [tier.get("points") for tier in self.tiers]
        if not all(self.role_ids) or any(value is None for value in points):
            await interaction.response.send_message(
                "Pick a role and set points for all three tiers first.", ephemeral=True
            )
            return
        if not (points[0] < points[1] < points[2]):
            await interaction.response.send_message(
                "Tier points must increase from tier 1 to tier 3.", ephemeral=True
            )
            return
        roles = [self.guild.get_role(role_id) for role_id in self.role_ids]
        if any(role is None for role in roles):
            await interaction.response.send_message(
                "One of those roles no longer exists — pick it again.", ephemeral=True
            )
            return
        await interaction.response.defer()
        for index in range(3):
            tier = config["giveaway_role_tiers"][index]
            tier["role_id"] = roles[index].id
            tier["points"] = points[index]
        save()
        failures = await apply_all_host_roles(self.guild)
        text = (
            "Saved. Roles are assigned at "
            f"{points[0]}, {points[1]} and {points[2]} points."
        )
        if failures:
            text += f"\nCouldn't update {failures} host(s) — check my role position."
        await interaction.edit_original_response(
            content=text, view=GiveawayRolesView(self.guild)
        )


class TierPointsModal(discord.ui.Modal):
    """Set the minimum points for one tier."""

    def __init__(self, view: GiveawayRolesView, index: int):
        super().__init__(title=f"Tier {index + 1} points")
        self.view = view
        self.index = index
        current = view.tiers[index].get("points")
        self.points_input = discord.ui.TextInput(
            label=f"Minimum points for tier {index + 1}",
            placeholder="For example 150",
            max_length=9,
            required=True,
            default=str(current) if current is not None else None,
        )
        self.add_item(self.points_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.points_input.value.strip()
        if not raw.isdigit():
            await interaction.response.send_message(
                "Points must be a whole number.", ephemeral=True
            )
            return
        self.view.tiers[self.index]["points"] = int(raw)
        self.view.refresh_points_labels()
        await interaction.response.edit_message(view=self.view)


# --------------------------------------------------------------------------- commands


giveaway_group = app_commands.Group(name="giveaway", description="Create and manage giveaways")


@giveaway_group.command(name="create", description="Start a giveaway")
@app_commands.describe(
    title="Giveaway title",
    duration="How long it runs, for example 30m, 2h or 7d",
    winners="How many winners to draw",
    host="User hosting the giveaway (ID or mention)",
    value="Host points for this giveaway, for example 150 or 150p",
    image="Optional image attachment shown in the embed",
    channel="Optional channel override (defaults to the giveaway channel in setup)",
)
async def giveaway_create(
    interaction: discord.Interaction,
    title: str,
    duration: str,
    winners: app_commands.Range[int, 1, 100],
    host: discord.User,
    value: str,
    image: discord.Attachment | None = None,
    channel: discord.TextChannel | None = None,
) -> None:
    if interaction.guild is None or not is_manager(interaction.user):
        await interaction.response.send_message(
            "Only giveaway managers can create giveaways.", ephemeral=True
        )
        return
    title = title.strip()
    if not title:
        await interaction.response.send_message("The title can't be empty.", ephemeral=True)
        return
    length = parse_duration(duration)
    if length is None or length <= dt.timedelta(0):
        await interaction.response.send_message(
            "Duration must look like `30s`, `30m`, `2h` or `7d`.", ephemeral=True
        )
        return
    points = parse_value(value)
    if points is None:
        await interaction.response.send_message(
            "Value must be a positive number, like `150` or `150p`.", ephemeral=True
        )
        return
    if image is not None and not (image.content_type or "").startswith("image/"):
        await interaction.response.send_message(
            "The image must be an image file.", ephemeral=True
        )
        return
    target = channel or interaction.guild.get_channel(config["giveaway_channel_id"])
    if target is None:
        await interaction.response.send_message(
            "Giveaways aren't set up yet. Ask an admin to run `/setup` → **Giveaways**.",
            ephemeral=True,
        )
        return
    role = (
        interaction.guild.get_role(config["giveaway_ping_role_id"])
        if config["giveaway_ping_role_id"]
        else None
    )
    giveaway_id = uuid.uuid4().hex[:8]
    now = dt.datetime.now(dt.timezone.utc)
    giveaway = {
        "title": title[:256],
        "host_id": host.id,
        "value": points,
        "winner_count": winners,
        "image": None,
        "channel_id": target.id,
        "message_id": None,
        "jump_url": None,
        "created_by_id": interaction.user.id,
        "created_at": now.isoformat(),
        "ends_at": (now + length).isoformat(),
        "status": "active",
        "entries": [],
        "winners": [],
        "past_winners": [],
    }
    # Store it before posting so the first click already finds the giveaway.
    config["giveaways"][giveaway_id] = giveaway
    await interaction.response.defer(ephemeral=True)
    # Upload the image with each message and reference it as attachment:// so the
    # embed keeps working long after Discord's signed attachment URLs expire.
    image_file = None
    log_file = None
    if image is not None:
        try:
            data = await image.read()
        except discord.HTTPException:
            config["giveaways"].pop(giveaway_id, None)
            await interaction.followup.send(
                "Couldn't download that attachment — try again.", ephemeral=True
            )
            return
        giveaway["image"] = f"attachment://{image.filename}"
        image_file = discord.File(io.BytesIO(data), filename=image.filename)
        log_file = discord.File(io.BytesIO(data), filename=image.filename)
    try:
        message = await target.send(
            content=role.mention if role else None,
            embed=build_embed(giveaway),
            view=GiveawayEntryView(giveaway_id),
            file=image_file,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=[role] if role else [], users=False
            ),
        )
    except discord.HTTPException:
        config["giveaways"].pop(giveaway_id, None)
        await interaction.followup.send(
            "I couldn't post the giveaway — check my permissions in that channel.",
            ephemeral=True,
        )
        return
    giveaway["message_id"] = message.id
    giveaway["jump_url"] = message.jump_url
    log_message = await log_giveaway(interaction.client, giveaway, log_file)
    if log_message is not None:
        giveaway["log_channel_id"] = config["giveaway_log_channel_id"]
        giveaway["log_message_id"] = log_message.id
    save()
    interaction.client.add_view(GiveawayEntryView(giveaway_id))
    await interaction.followup.send(
        f"Giveaway started in {target.mention}: {message.jump_url}", ephemeral=True
    )


@giveaway_group.command(name="manage", description="Show active giveaways and end one now")
async def giveaway_manage(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not is_manager(interaction.user):
        await interaction.response.send_message(
            "Only giveaway managers can manage giveaways.", ephemeral=True
        )
        return
    if not any(
        giveaway.get("status") == "active" for giveaway in config["giveaways"].values()
    ):
        await interaction.response.send_message(
            "There are no active giveaways.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        "**Active giveaways** — pick one, then **End now**.",
        view=GiveawayManageView(),
        ephemeral=True,
    )


def build_points_embed(user_id: int) -> discord.Embed:
    hosted = sum(
        1
        for giveaway in config["giveaways"].values()
        if giveaway.get("host_id") == user_id
    )
    embed = discord.Embed(title="Giveaway points", color=discord.Color(GOLD))
    embed.description = f"<@{user_id}>"
    embed.add_field(name="Points", value=str(points_for(user_id)), inline=True)
    embed.add_field(name="Giveaways hosted", value=str(hosted), inline=True)
    embed.set_footer(text="Points are added when a giveaway is marked done.")
    return embed


def _giveaway_status(giveaway: dict) -> str:
    if giveaway.get("points_revoked"):
        return "points revoked"
    if giveaway.get("status") == "done":
        return "done"
    if giveaway.get("status") == "ended":
        return "ended, not done"
    return "running"


class GiveawayInfoView(discord.ui.View):
    """A host's giveaways, with the option to revoke a done giveaway's points."""

    def __init__(self, guild: discord.Guild, user_id: int, can_revoke: bool):
        super().__init__(timeout=900)
        self.guild = guild
        self.user_id = user_id
        self.selected: str | None = None
        self.giveaways = sorted(
            (
                (giveaway_id, giveaway)
                for giveaway_id, giveaway in config["giveaways"].items()
                if giveaway.get("host_id") == user_id
            ),
            key=lambda pair: pair[1].get("created_at", ""),
            reverse=True,
        )
        select = discord.ui.Select(
            placeholder="Pick a giveaway",
            options=[
                discord.SelectOption(
                    label=giveaway["title"][:100] or "Untitled",
                    value=giveaway_id,
                    description=(
                        f"{giveaway.get('value', 0)} pts · {_giveaway_status(giveaway)}"
                    )[:100],
                )
                for giveaway_id, giveaway in self.giveaways[:25]
            ],
        )
        select.callback = self._picked
        self.add_item(select)
        self.revoke_button: discord.ui.Button | None = None
        if can_revoke:
            self.revoke_button = discord.ui.Button(
                label="Revoke points", style=discord.ButtonStyle.danger, row=1
            )
            self.revoke_button.callback = self._toggle_points
            self.add_item(self.revoke_button)

    async def _picked(self, interaction: discord.Interaction) -> None:
        self.selected = interaction.data["values"][0]
        if self.revoke_button is not None:
            revoked = config["giveaways"].get(self.selected, {}).get("points_revoked")
            self.revoke_button.label = "Restore points" if revoked else "Revoke points"
            self.revoke_button.style = (
                discord.ButtonStyle.secondary if revoked else discord.ButtonStyle.danger
            )
        await interaction.response.edit_message(view=self)

    async def _toggle_points(self, interaction: discord.Interaction) -> None:
        if self.selected is None:
            await interaction.response.send_message(
                "Pick a giveaway first.", ephemeral=True
            )
            return
        giveaway = config["giveaways"].get(self.selected)
        if giveaway is None:
            await interaction.response.send_message(
                "That giveaway no longer exists.", ephemeral=True
            )
            return
        revoked = giveaway.get("points_revoked", False)
        if not revoked and giveaway.get("status") != "done":
            await interaction.response.send_message(
                "That giveaway hasn't been marked done yet — there are no points to revoke.",
                ephemeral=True,
            )
            return
        giveaway["points_revoked"] = not revoked
        save()
        roles_ok = await apply_host_roles(self.guild, self.user_id)
        word = "Restored" if revoked else "Revoked"
        text = f"{word} the {giveaway['value']} points for **{giveaway['title']}**."
        if not roles_ok:
            text += "\nCouldn't update the host's roles — check my role position."
        await interaction.response.edit_message(
            content=text,
            embed=build_points_embed(self.user_id),
            view=GiveawayInfoView(self.guild, self.user_id, True),
        )


@app_commands.command(name="gwinfo", description="Show giveaway points for a host")
@app_commands.describe(user="User to look up (default: you)")
async def gwinfo(interaction: discord.Interaction, user: discord.User | None = None) -> None:
    target = user or interaction.user
    hosted = any(
        giveaway.get("host_id") == target.id for giveaway in config["giveaways"].values()
    )
    embed = build_points_embed(target.id)
    if hosted and interaction.guild is not None:
        view = GiveawayInfoView(
            interaction.guild, target.id, permissions.is_admin(interaction.user)
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


@app_commands.command(name="gwroles", description="Configure the giveaway points roles")
async def gwroles(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not permissions.is_admin(interaction.user):
        await interaction.response.send_message(
            "Only administrators can configure giveaway roles.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        "**Giveaway roles** — pick a role per tier, set the points each tier needs, "
        "then **Save & apply**.",
        view=GiveawayRolesView(interaction.guild),
        ephemeral=True,
    )


def register(bot: discord.Client) -> None:
    """Add the giveaway commands and re-attach the panels after every restart."""
    if bot.tree.get_command("giveaway") is None:
        bot.tree.add_command(giveaway_group)
    for command in (gwinfo, gwroles):
        if bot.tree.get_command(command.name) is None:
            bot.tree.add_command(command)
    for giveaway_id, giveaway in config["giveaways"].items():
        if giveaway.get("status") == "ended":
            bot.add_view(GiveawayRerollView(giveaway_id))
        elif giveaway.get("status") == "done":
            bot.add_view(GiveawayRerollView(giveaway_id, done=True))
        else:
            bot.add_view(GiveawayEntryView(giveaway_id))
