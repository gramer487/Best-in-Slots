"""UI components: role panel, LFG panel, LFG form and the /setup pages."""

from __future__ import annotations

import datetime as dt
import re

import aiohttp
import discord

from applications import ApplicationsSetupView
from config import config, save
from modmail import ModmailSetupView
from permissions import AdminOnlyView
from suggestions import SuggestionSetupView
from voicehub import VoiceSetupView
from worldstate import fetch_worldstate

PANEL_INTRO = "Pick a role to ping, how many players you need and for how long — then hit **Post LFG**.\n\n"

# Hardcoded per-role player limits: this role allows up to 5, everything else 3.
FIVE_PLAYER_ROLE_ID = 1559600840950423632
REGIONS = ("NA", "EU", "SA", "AS", "AU")
LFG_LIFETIME = dt.timedelta(hours=2)
LFG_CLOSE_DELAY = dt.timedelta(minutes=10)
MAX_LFG_LIFETIME = dt.timedelta(days=14)
# Discord allows at most five rows of five buttons in a single message.
MAX_ROLE_BUTTONS = 25
_EXPIRY_ARG = re.compile(r"^(\d+)\s*([mhd])$")
_EXPIRY_UNITS = {
    "m": dt.timedelta(minutes=1),
    "h": dt.timedelta(hours=1),
    "d": dt.timedelta(days=1),
}


def parse_expiry(text: str) -> dt.timedelta | None:
    """Parse a thread lifetime like '30m', '1h' or '7d'; None if it isn't one."""
    match = _EXPIRY_ARG.fullmatch(text.strip().lower())
    if match is None:
        return None
    return int(match.group(1)) * _EXPIRY_UNITS[match.group(2)]


def thread_expiry(entry: dict) -> dt.datetime:
    """When the thread is deleted: its custom expiry if set, else 2 hours after creation."""
    if entry.get("expires_at"):
        return dt.datetime.fromisoformat(entry["expires_at"])
    return dt.datetime.fromisoformat(entry["created"]) + LFG_LIFETIME


def _summary(role, count, duration, region, note) -> str:
    return (
        f"**Role to ping:** {role.name if role else '❌ not set'}\n"
        f"**Players needed:** {count if count else '❌ not set'}\n"
        f"**Duration:** {duration or '❌ not set'}\n"
        f"**Region:** {region or '❌ not set'}\n"
        f"**Note:** {note or '—'}"
    )


class RolePanelView(discord.ui.View):
    """Embed 1: one button per configured role; pressing it toggles that role."""

    def __init__(self, guild: discord.Guild, roles: list[discord.Role | None] | None = None):
        super().__init__(timeout=None)
        if roles is None:
            roles = [guild.get_role(rid) if rid else None for rid in config["button_roles"]]
        seen: set[int] = set()
        for role in roles:
            if role is None or role.id in seen:
                continue
            seen.add(role.id)
            button = discord.ui.Button(
                label=role.name[:80],
                style=discord.ButtonStyle.blurple,
                custom_id=f"role_button_{role.id}",
            )
            button.callback = self._toggle_callback(role.id)
            self.add_item(button)

    def _toggle_callback(self, role_id: int):
        async def toggle(interaction: discord.Interaction) -> None:
            await self._toggle(interaction, role_id)

        return toggle

    async def _toggle(self, interaction: discord.Interaction, role_id: int) -> None:
        role = interaction.guild.get_role(role_id)
        if role is None:
            await interaction.response.send_message(
                "That role isn't configured anymore.", ephemeral=True
            )
            return
        if role in interaction.user.roles:
            await interaction.user.remove_roles(role)
            text = f"Removed **{role.name}**"
        else:
            try:
                await interaction.user.add_roles(role)
            except discord.Forbidden:
                await interaction.response.send_message(
                    "I can't give that role — check my role hierarchy/permissions.", ephemeral=True
                )
                return
            text = f"Added **{role.name}**"
        await interaction.response.send_message(text, ephemeral=True)


class LfgPanelView(discord.ui.View):
    """Embed 2: button that opens the ephemeral LFG form."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Start LFG", style=discord.ButtonStyle.green, emoji="🔎", custom_id="start_lfg")
    async def start_lfg(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        roles = [r for rid in config["ping_roles"] if (r := interaction.guild.get_role(rid))]
        if not roles:
            await interaction.response.send_message(
                "No pingable roles are configured yet. Ask an admin to run `/setup`.",
                ephemeral=True,
            )
            return
        view = LfgSetupView(roles)
        await interaction.response.send_message(PANEL_INTRO + view.summary(), view=view, ephemeral=True)


async def post_panels(channel, guild, button_roles=None):
    """Post the self-role and LFG panels in the channel. Returns (role_message, lfg_message)."""
    role_view = RolePanelView(guild, button_roles)
    role_message = await channel.send(
        embed=discord.Embed(
            title="Self roles",
            description="Select or remove the role(s) that you want to be pinged in LFG posts.",
            color=discord.Color.blurple(),
        ),
        view=role_view if role_view.children else None,
    )
    lfg_message = await channel.send(
        embed=discord.Embed(
            title="Looking for Group",
            description=(
                "Click **Start LFG** below to open a form.\n"
                "Your post will ping a role, open a thread and the thread expires after 2 hours."
            ),
            color=discord.Color.green(),
        ),
        view=LfgPanelView(),
    )
    return role_message, lfg_message


def build_lfg_embed(guild: discord.Guild, entry: dict) -> discord.Embed:
    """The public LFG post. Rebuilt whenever the player count or lineup changes."""
    role = guild.get_role(entry["role_id"]) if entry.get("role_id") else None
    embed = discord.Embed(
        title=f"LFG — {role.name}" if role else "LFG", color=discord.Color.green()
    )
    if role is not None:
        embed.add_field(name="Role", value=role.mention, inline=True)
    needed = "Full" if entry.get("full") else entry.get("players_needed", "—")
    embed.add_field(name="Players needed", value=str(needed), inline=True)
    embed.add_field(name="Duration", value=entry.get("duration") or "—", inline=True)
    # Mentions render as clickable names; allowed_mentions keeps edits from pinging.
    embed.add_field(name="Host", value=f"<@{entry['host']}>", inline=True)
    embed.add_field(name="Region", value=entry.get("region") or "—", inline=True)
    player_ids = [entry["host"], *entry.get("players", [])]
    players = ", ".join(f"<@{pid}>" for pid in player_ids)
    embed.add_field(name="Players", value=players[:1024], inline=False)
    if entry.get("note"):
        embed.add_field(name="Note", value=entry["note"], inline=False)
    expires = thread_expiry(entry)
    # Discord timestamps render in each viewer's local timezone.
    embed.add_field(
        name="Thread expires",
        value=f"<t:{int(expires.timestamp())}:F> (<t:{int(expires.timestamp())}:R>)",
        inline=False,
    )
    if entry.get("delete_at"):
        closing = dt.datetime.fromisoformat(entry["delete_at"])
        embed.add_field(name="Closing", value=f"<t:{int(closing.timestamp())}:R>", inline=False)
    return embed


def build_control_embed(entry: dict) -> discord.Embed:
    """The owner-only control panel posted inside the LFG thread."""
    embed = discord.Embed(
        title="LFG controls",
        description=(
            "Only the thread owner can use these buttons.\n"
            "React ✅ to a player's message to mark them as joined; remove the reaction to undo it.\n"
            "Add or remove players manually with `$add @user` or `$remove @user`."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Players needed",
        value="Full" if entry.get("full") else str(entry.get("players_needed", "—")),
        inline=True,
    )
    embed.add_field(name="Region", value=entry.get("region") or "—", inline=True)
    players = ", ".join(f"<@{pid}>" for pid in entry.get("players", [])) or "—"
    embed.add_field(name="Marked players", value=players[:1024], inline=False)
    if entry.get("delete_at"):
        closing = dt.datetime.fromisoformat(entry["delete_at"])
        embed.add_field(name="Closing", value=f"<t:{int(closing.timestamp())}:R>", inline=False)
    return embed


async def refresh_lfg_messages(bot, thread_id, entry: dict) -> None:
    """Re-render the public LFG post and the in-thread control panel after a change."""
    guild = bot.get_guild(config["guild_id"])
    if guild is None:
        return
    channel = guild.get_channel(entry.get("post_channel_id") or 0)
    if channel is not None and entry.get("post_message_id"):
        try:
            message = await channel.fetch_message(entry["post_message_id"])
            await message.edit(
                embed=build_lfg_embed(guild, entry),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            pass
    thread = guild.get_channel(int(thread_id))
    if thread is None:
        try:
            thread = await guild.fetch_channel(int(thread_id))
        except discord.HTTPException:
            thread = None
    if isinstance(thread, discord.Thread) and entry.get("control_message_id"):
        try:
            panel = await thread.fetch_message(entry["control_message_id"])
            await panel.edit(embed=build_control_embed(entry))
        except discord.HTTPException:
            pass


class LfgControlView(discord.ui.View):
    """Persistent thread-owner controls posted at the top of each LFG thread."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _owner_entry(self, interaction: discord.Interaction) -> dict | None:
        entry = config["threads"].get(str(interaction.channel_id))
        if not isinstance(entry, dict) or not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "These controls only work inside an LFG thread.", ephemeral=True
            )
            return None
        if interaction.user.id != entry.get("host"):
            await interaction.response.send_message(
                "Only the thread owner can use these controls.", ephemeral=True
            )
            return None
        return entry

    async def _finish(self, interaction, entry, text) -> None:
        save()
        await refresh_lfg_messages(interaction.client, interaction.channel_id, entry)
        await interaction.followup.send(text, ephemeral=True)

    @discord.ui.button(label="Set expiry", style=discord.ButtonStyle.grey, custom_id="lfg_expiry")
    async def expiry(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._owner_entry(interaction) is None:
            return
        await interaction.response.send_modal(ExpiryModal())

    @discord.ui.button(label="Need one fewer", style=discord.ButtonStyle.grey, custom_id="lfg_dec")
    async def dec(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        entry = await self._owner_entry(interaction)
        if entry is None:
            return
        await interaction.response.defer(ephemeral=True)
        entry["players_needed"] = max(0, entry.get("players_needed", 0) - 1)
        if entry["players_needed"] == 0:
            entry["full"] = True
        await self._finish(interaction, entry, f"Players needed: {entry['players_needed']}.")

    @discord.ui.button(label="Need one more", style=discord.ButtonStyle.grey, custom_id="lfg_inc")
    async def inc(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        entry = await self._owner_entry(interaction)
        if entry is None:
            return
        await interaction.response.defer(ephemeral=True)
        entry["players_needed"] = entry.get("players_needed", 0) + 1
        entry["full"] = False
        await self._finish(interaction, entry, f"Players needed: {entry['players_needed']}.")

    @discord.ui.button(label="Mark as full", style=discord.ButtonStyle.green, custom_id="lfg_full")
    async def full(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        entry = await self._owner_entry(interaction)
        if entry is None:
            return
        await interaction.response.defer(ephemeral=True)
        entry["players_needed"] = 0
        entry["full"] = True
        await self._finish(interaction, entry, "Marked as full.")

    @discord.ui.button(label="Close thread", style=discord.ButtonStyle.red, custom_id="lfg_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        entry = await self._owner_entry(interaction)
        if entry is None:
            return
        await interaction.response.defer(ephemeral=True)
        if entry.get("delete_at"):
            await interaction.followup.send("This thread is already closing.", ephemeral=True)
            return
        entry["delete_at"] = (dt.datetime.now(dt.timezone.utc) + LFG_CLOSE_DELAY).isoformat()
        save()
        try:
            await interaction.channel.send("This thread will be deleted in 10 minutes.")
        except discord.HTTPException:
            pass
        await refresh_lfg_messages(interaction.client, interaction.channel_id, entry)
        await interaction.followup.send(
            "This thread will be deleted in 10 minutes.", ephemeral=True
        )


class ExpiryModal(discord.ui.Modal, title="Custom thread expiry"):
    lifetime = discord.ui.TextInput(
        label="Expire in",
        placeholder="30m 1h or 7d",
        max_length=10,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        entry = config["threads"].get(str(interaction.channel_id))
        if entry is None:
            await interaction.response.send_message(
                "This thread is no longer active.", ephemeral=True
            )
            return
        duration = parse_expiry(str(self.lifetime.value))
        if duration is None or duration <= dt.timedelta(0):
            await interaction.response.send_message(
                "Use a number and a unit, like 30m, 1h or 7d.", ephemeral=True
            )
            return
        if duration > MAX_LFG_LIFETIME:
            await interaction.response.send_message(
                "The longest custom expiry is 14 days.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        expires = dt.datetime.now(dt.timezone.utc) + duration
        entry["expires_at"] = expires.isoformat()
        save()
        await refresh_lfg_messages(interaction.client, interaction.channel_id, entry)
        await interaction.followup.send(
            f"This thread will now expire <t:{int(expires.timestamp())}:R>.",
            ephemeral=True,
        )


async def handle_lfg_reaction(bot, payload: discord.RawReactionActionEvent, *, removed: bool) -> None:
    """Let the host mark a player by adding/removing ✅ on that player's thread message."""
    if str(payload.emoji) != "✅" or payload.guild_id != config["guild_id"]:
        return
    entry = config["threads"].get(str(payload.channel_id))
    if not isinstance(entry, dict) or "players_needed" not in entry:
        return
    if payload.user_id != entry.get("host"):
        return
    channel = bot.get_channel(payload.channel_id)
    if not isinstance(channel, discord.Thread):
        return
    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.HTTPException:
        return
    if message.author.bot or message.author.id == entry.get("host"):
        return
    players = entry.setdefault("players", [])
    if removed:
        if message.author.id not in players:
            return
        players.remove(message.author.id)
        entry["players_needed"] = entry.get("players_needed", 0) + 1
        entry["full"] = False
    else:
        if message.author.id in players:
            return
        players.append(message.author.id)
        entry["players_needed"] = max(0, entry.get("players_needed", 0) - 1)
        if entry["players_needed"] == 0:
            entry["full"] = True
    save()
    await refresh_lfg_messages(bot, payload.channel_id, entry)


async def handle_lfg_command(bot, message: discord.Message) -> bool:
    """Handle "$add @user" and "$remove @user" typed by the thread owner."""
    command, _, _ = message.content.strip().partition(" ")
    command = command.lower()
    if command not in ("$add", "$remove") or not isinstance(message.channel, discord.Thread):
        return False
    entry = config["threads"].get(str(message.channel.id))
    if not isinstance(entry, dict):
        return False
    if entry.get("host") is not None and message.author.id != entry["host"]:
        return True  # only the thread owner manages the team
    target = message.mentions[0] if message.mentions else None
    if target is None:
        await message.channel.send(f"Use `{command} @user` and mention the player.")
        return True
    if target.id == entry.get("host"):
        await message.channel.send("That's you — the host is already on the team.")
        return True
    players = entry.setdefault("players", [])
    if command == "$add":
        if target.id in players:
            await message.channel.send(f"**{target.display_name}** is already on the team.")
            return True
        players.append(target.id)
        entry["players_needed"] = max(0, entry.get("players_needed", 0) - 1)
        entry["full"] = entry["players_needed"] == 0
        text = f"✅ Added **{target.display_name}** to the team."
    else:
        if target.id not in players:
            await message.channel.send(f"**{target.display_name}** isn't on the team.")
            return True
        players.remove(target.id)
        entry["players_needed"] = entry.get("players_needed", 0) + 1
        entry["full"] = False
        text = f"➖ Removed **{target.display_name}** from the team."
    save()
    await refresh_lfg_messages(bot, message.channel.id, entry)
    await message.channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    return True


def build_status_embed(data: dict) -> discord.Embed:
    """Embed 3: current arbitration and the Steel Path Void Cascade flag."""
    embed = discord.Embed(title="World state", color=discord.Color.orange())
    arbitration = data["arbitration"]
    if arbitration:
        tier = f" ({arbitration['tier']} Tier)" if arbitration["tier"] != "?" else ""
        resets = int(arbitration["rotates_at"].timestamp())
        embed.add_field(
            name="Arbitration",
            value=(
                f"**{arbitration['name']}** ({arbitration['system']}) — "
                f"{arbitration['mission']}{tier}\n"
                f"Resets (<t:{resets}:R>)"
            ),
            inline=True,
        )
    else:
        embed.add_field(name="Arbitration", value="❌ No data", inline=True)
    sp = data["sp_cascade"]
    if sp:
        expires = int(sp["expiry"].timestamp())
        embed.add_field(
            name="SP Void Cascade",
            value=(
                f"✅ True — {sp['tier']} · **{sp['name']}** ({sp['system']})\n"
                f"Expires  (<t:{expires}:R>)"
            ),
            inline=True,
        )
    else:
        embed.add_field(name="SP Void Cascade", value="❌ False", inline=True)
    embed.set_footer(text="browse.wf")
    return embed


async def refresh_status(channel) -> dict | None:
    """Fetch the world state and post/update the status embed. Returns the data, or None."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            data = await fetch_worldstate(session)
    except (aiohttp.ClientError, ValueError) as e:
        print(f"World state fetch failed: {e}")
        return None
    message = None
    if config["status_message_id"]:
        try:
            message = await channel.fetch_message(config["status_message_id"])
        except discord.NotFound:
            message = None  # deleted — repost
    try:
        if message is None:
            message = await channel.send(embed=build_status_embed(data))
            config["status_message_id"] = message.id
            save()
        else:
            await message.edit(embed=build_status_embed(data))
    except discord.HTTPException as e:
        print(f"Status embed update failed: {e}")
    return data


class LfgSetupView(discord.ui.View):
    """Ephemeral LFG form: role to ping, player count, duration, region, optional note."""

    def __init__(self, roles: list[discord.Role]):
        super().__init__(timeout=600)
        self.role: discord.Role | None = None
        self.count: int | None = None
        self.duration: str | None = None
        self.region: str | None = None
        self.note: str | None = None
        self.roles = roles
        self.role_page = 0

        self.role_select = discord.ui.Select(
            placeholder="Which role should be pinged?",
            options=self._role_options(),
            row=0,
        )
        self.role_select.callback = self.on_role
        self.add_item(self.role_select)

        self.count_select = discord.ui.Select(
            placeholder="How many players do you need?",
            options=self._count_options(),
            row=1,
        )
        self.count_select.callback = self.on_count
        self.add_item(self.count_select)

        self.duration_select = discord.ui.Select(
            placeholder="How long will you play?",
            options=[
                discord.SelectOption(label="30 minutes", value="30m"),
                discord.SelectOption(label="1 hour", value="1h"),
                discord.SelectOption(label="2 hours", value="2h"),
                discord.SelectOption(label="3 hours", value="3h"),
                discord.SelectOption(label="5+ hours", value="5h+"),
            ],
            row=2,
        )
        self.duration_select.callback = self.on_duration
        self.add_item(self.duration_select)

        self.region_select = discord.ui.Select(
            placeholder="Which region are you playing in?",
            options=[discord.SelectOption(label=region, value=region) for region in REGIONS],
            row=3,
        )
        self.region_select.callback = self.on_region
        self.add_item(self.region_select)

    def summary(self) -> str:
        return _summary(self.role, self.count, self.duration, self.region, self.note)

    def _count_options(self) -> list[discord.SelectOption]:
        max_players = 5 if self.role is not None and self.role.id == FIVE_PLAYER_ROLE_ID else 3
        return [
            discord.SelectOption(label=f"{n} player{'s' if n != 1 else ''}", value=str(n))
            for n in range(1, max_players + 1)
        ]

    def _role_options(self) -> list[discord.SelectOption]:
        # A select holds at most 25 options; keep two free for page navigation.
        per_page = 23 if len(self.roles) > 25 else 25
        start = self.role_page * per_page
        options = [
            discord.SelectOption(label=role.name, value=str(role.id))
            for role in self.roles[start:start + per_page]
        ]
        if self.role_page > 0:
            options.append(discord.SelectOption(label="⬅ Previous roles", value="__prev__"))
        if start + per_page < len(self.roles):
            options.append(discord.SelectOption(label="➡ More roles", value="__more__"))
        return options

    async def on_role(self, interaction: discord.Interaction) -> None:
        value = self.role_select.values[0]
        if value in ("__prev__", "__more__"):
            self.role_page += -1 if value == "__prev__" else 1
            self.role_select.options = self._role_options()
            await self._refresh(interaction)
            return
        self.role = interaction.guild.get_role(int(value))
        # The player limit depends on the role — rebuild the options and clear the old count.
        self.count_select.options = self._count_options()
        self.count = None
        await self._refresh(interaction)

    async def on_count(self, interaction: discord.Interaction) -> None:
        self.count = int(self.count_select.values[0])
        await self._refresh(interaction)

    async def on_duration(self, interaction: discord.Interaction) -> None:
        self.duration = self.duration_select.values[0]
        await self._refresh(interaction)

    async def on_region(self, interaction: discord.Interaction) -> None:
        self.region = self.region_select.values[0]
        await self._refresh(interaction)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        # Ephemeral messages can only be edited through the interaction response —
        # plain Message.edit would 404. The view rides along so rebuilt select
        # options (per-role player limit) show up.
        await interaction.response.edit_message(content=PANEL_INTRO + self.summary(), view=self)

    @discord.ui.button(label="Add a note", style=discord.ButtonStyle.grey, emoji="📝", row=4)
    async def add_note(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(NoteModal(view=self))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, row=4)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.delete_message()

    @discord.ui.button(label="Post LFG", style=discord.ButtonStyle.green, row=4)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if (
            self.role is None
            or self.count is None
            or self.duration is None
            or self.region is None
        ):
            await interaction.response.send_message(
                "Pick a role, a player count, a duration and a region first.", ephemeral=True
            )
            return
        channel = interaction.guild.get_channel(config["channel_id"])
        if channel is None or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "The LFG channel isn't configured. Ask an admin to run `/setup`.", ephemeral=True
            )
            return
        me = interaction.guild.me
        if me is not None:
            perms = channel.permissions_for(me)
            missing = [
                label
                for label, allowed in (
                    ("Send Messages", perms.send_messages),
                    ("Embed Links", perms.embed_links),
                    ("Create Public Threads", perms.create_public_threads),
                    ("Send Messages in Threads", perms.send_messages_in_threads),
                )
                if not allowed
            ]
            if missing:
                await interaction.response.send_message(
                    "I'm missing these permissions in the LFG channel: "
                    + ", ".join(missing)
                    + ". Check the channel's permission overwrites for my role.",
                    ephemeral=True,
                )
                return
        created = dt.datetime.now(dt.timezone.utc)
        entry = {
            "created": created.isoformat(),
            "host": interaction.user.id,
            "role_id": self.role.id,
            "players_needed": self.count,
            "region": self.region,
            "duration": self.duration,
            "note": self.note,
            "players": [],
            "full": False,
            "post_channel_id": channel.id,
            "post_message_id": None,
            "control_message_id": None,
        }
        # Posting, thread creation and the control panel are several API calls; without
        # this the interaction token can expire before the final edit (error 10062).
        await interaction.response.defer()
        try:
            message = await channel.send(
                content=self.role.mention,
                embed=build_lfg_embed(interaction.guild, entry),
                allowed_mentions=discord.AllowedMentions(roles=[self.role]),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to post in the LFG channel.", ephemeral=True
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"Posting the LFG failed: {e}", ephemeral=True
            )
            return
        entry["post_message_id"] = message.id
        try:
            thread = await message.create_thread(
                name=f"LFG — {self.role.name} ({self.count} needed)"[:100],
                # Keep long-lived threads visible: 60 minutes would hide them from the
                # channel list, and 1440 (24 hours) would archive them after a day idle.
                # 10080 (7 days) is Discord's maximum.
                auto_archive_duration=10080,
            )
        except (discord.HTTPException, ValueError) as e:
            # Don't leave an orphan post with no thread to talk in.
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            await interaction.followup.send(
                f"Couldn't open the thread: {e}. Check that I have **Create Public Threads** "
                "and **Send Messages in Threads** in the LFG channel.",
                ephemeral=True,
            )
            return
        panel_note = ""
        try:
            panel = await thread.send(embed=build_control_embed(entry), view=LfgControlView())
            entry["control_message_id"] = panel.id
        except discord.HTTPException as e:
            panel_note = f"\nI couldn't post the control panel in the thread: {e}"
        config["threads"][str(thread.id)] = entry
        save()
        self.clear_items()
        self.stop()
        await interaction.edit_original_response(
            content=f"✅ Posted! {thread.jump_url}{panel_note}", view=self
        )


class NoteModal(discord.ui.Modal, title="LFG note"):
    note = discord.ui.TextInput(
        label="Note",
        style=discord.TextStyle.paragraph,
        placeholder="Anything specific? (optional)",
        max_length=500,
        required=False,
    )

    def __init__(self, view: LfgSetupView):
        super().__init__()
        self.parent_view = view
        self.note.default = view.note or ""

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.parent_view.note = str(self.note.value).strip() or None
        # Ephemeral messages can only be edited through the interaction response.
        await interaction.response.edit_message(content=PANEL_INTRO + self.parent_view.summary())


class SetupMenuView(AdminOnlyView):
    """First /setup page: pick which system to configure."""

    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=300)
        self.guild = guild

    @discord.ui.button(label="LFG", style=discord.ButtonStyle.blurple, emoji="🔎", row=0)
    async def lfg(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = SetupChannelView(SetupState(), interaction.guild)
        await interaction.response.edit_message(content=view.content(), view=view)

    @discord.ui.button(label="Modmail", style=discord.ButtonStyle.blurple, emoji="📨", row=0)
    async def modmail(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="**Modmail setup** — panel channel, ping roles, moderator role and log forum.",
            view=ModmailSetupView(interaction.guild),
        )

    @discord.ui.button(label="Suggestions", style=discord.ButtonStyle.blurple, emoji="💡", row=0)
    async def suggestions(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="**Suggestion setup** — where suggestion posts go and which role is pinged.",
            view=SuggestionSetupView(interaction.guild),
        )

    @discord.ui.button(label="Applications", style=discord.ButtonStyle.blurple, emoji="📋", row=0)
    async def applications(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="**Applications setup** — channels, panels and active applications.",
            view=ApplicationsSetupView(interaction.guild),
        )

    @discord.ui.button(label="VC create", style=discord.ButtonStyle.blurple, emoji="🔊", row=0)
    async def voice(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="**Voice setup** — pick the hub voice channel, the activity log "
            "and the role Lock/Hide deny.",
            view=VoiceSetupView(interaction.guild),
        )


class SetupState:
    def __init__(self):
        self.channel: discord.TextChannel | None = None
        self.ping_roles: list[discord.Role] = []
        self.button_roles: list[discord.Role] = []
        self.admin_role: discord.Role | None = None


class SetupChannelView(AdminOnlyView):
    """Setup page 1: LFG channel, pingable roles and the admin role."""

    def __init__(self, state: SetupState, guild: discord.Guild):
        super().__init__(timeout=300)
        self.state = state
        # The role picker caps each pick at 25 roles, so selections accumulate here —
        # there is no limit on how many pingable roles can be configured.
        if state.ping_roles:
            self.ping_roles = list(state.ping_roles)
        else:
            self.ping_roles = [
                role
                for rid in config["ping_roles"]
                if (role := guild.get_role(rid)) is not None
            ]
        channel_default = (
            [guild.get_channel(config["channel_id"])]
            if config["channel_id"] and guild.get_channel(config["channel_id"])
            else []
        )
        self.channel_select = discord.ui.ChannelSelect(
            placeholder="LFG channel",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            row=0,
            default_values=channel_default,
        )
        self.channel_select.callback = self._ack
        self.add_item(self.channel_select)
        self.roles_select = discord.ui.RoleSelect(
            placeholder="Pick roles to add to the pingable list",
            min_values=1,
            max_values=25,
            row=1,
        )
        self.roles_select.callback = self._ack
        self.add_item(self.roles_select)
        admin_default = (
            [guild.get_role(config["admin_role_id"])]
            if config["admin_role_id"] and guild.get_role(config["admin_role_id"])
            else []
        )
        self.admin_select = discord.ui.RoleSelect(
            placeholder="Admin role for /setup (optional)",
            min_values=0,
            max_values=1,
            row=2,
            default_values=admin_default,
        )
        self.admin_select.callback = self._ack
        self.add_item(self.admin_select)

    async def _ack(self, interaction: discord.Interaction) -> None:
        # Selects are read when Add/Remove/Clear/Next is pressed — just acknowledge.
        await interaction.response.defer()

    def content(self) -> str:
        names = ", ".join(role.name for role in self.ping_roles) or "none"
        return (
            "**LFG setup** — page 1: LFG channel, pingable roles and the admin role.\n"
            f"**Pingable roles ({len(self.ping_roles)}):** {names}"
        )[:2000]

    @discord.ui.button(label="Add selected", style=discord.ButtonStyle.blurple, row=3)
    async def add_roles(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        picked = self.roles_select.values
        if not picked:
            await interaction.response.send_message(
                "Pick roles in the dropdown first.", ephemeral=True
            )
            return
        known = {role.id for role in self.ping_roles}
        for role in picked:
            if role.id not in known:
                self.ping_roles.append(role)
                known.add(role.id)
        await interaction.response.edit_message(content=self.content(), view=self)

    @discord.ui.button(label="Remove selected", style=discord.ButtonStyle.grey, row=3)
    async def remove_roles(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        picked = {role.id for role in self.roles_select.values}
        if not picked:
            await interaction.response.send_message(
                "Pick roles in the dropdown first.", ephemeral=True
            )
            return
        self.ping_roles = [role for role in self.ping_roles if role.id not in picked]
        await interaction.response.edit_message(content=self.content(), view=self)

    @discord.ui.button(label="Clear all", style=discord.ButtonStyle.grey, row=3)
    async def clear_roles(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.ping_roles = []
        await interaction.response.edit_message(content=self.content(), view=self)

    @discord.ui.button(label="Next →", style=discord.ButtonStyle.blurple, row=3)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Untouched selects return empty values — fall back to the shown defaults.
        channels = self.channel_select.values or self.channel_select.default_values
        if not channels or not self.ping_roles:
            await interaction.response.send_message(
                "Pick the LFG channel and add at least one pingable role first.",
                ephemeral=True,
            )
            return
        # ChannelSelect.values gives a partial channel — resolve it to the real object.
        self.state.channel = interaction.guild.get_channel(channels[0].id)
        self.state.ping_roles = list(self.ping_roles)
        admin = self.admin_select.values or self.admin_select.default_values
        self.state.admin_role = admin[0] if admin else None
        view = SetupRolesView(self.state, interaction.guild)
        await interaction.response.edit_message(content=view.content(), view=view)


class SetupRolesView(AdminOnlyView):
    """Setup page 2: the self-role buttons, then save & post the panels."""

    def __init__(self, state: SetupState, guild: discord.Guild):
        super().__init__(timeout=300)
        self.state = state
        # Like the pingable roles: picks accumulate, so the button count isn't
        # limited by the 25 roles a single picker interaction can carry.
        if state.button_roles:
            self.button_roles = list(state.button_roles)
        else:
            self.button_roles = [
                role
                for rid in config["button_roles"]
                if rid and (role := guild.get_role(rid)) is not None
            ]
        self.roles_select = discord.ui.RoleSelect(
            placeholder="Pick roles to add as self-role buttons",
            min_values=1,
            max_values=25,
            row=0,
        )
        self.roles_select.callback = self._ack
        self.add_item(self.roles_select)

    async def _ack(self, interaction: discord.Interaction) -> None:
        # The select is read when Add/Remove/Clear/Save is pressed — just acknowledge.
        await interaction.response.defer()

    def content(self) -> str:
        names = ", ".join(role.name for role in self.button_roles) or "none"
        return (
            "**LFG setup** — page 2: pick the roles for the self-role buttons.\n"
            f"**Buttons ({len(self.button_roles)}/{MAX_ROLE_BUTTONS}):** {names}"
        )[:2000]

    @discord.ui.button(label="Add selected", style=discord.ButtonStyle.blurple, row=1)
    async def add_roles(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        picked = self.roles_select.values
        if not picked:
            await interaction.response.send_message(
                "Pick roles in the dropdown first.", ephemeral=True
            )
            return
        known = {role.id for role in self.button_roles}
        new = [role for role in picked if role.id not in known]
        if len(self.button_roles) + len(new) > MAX_ROLE_BUTTONS:
            await interaction.response.send_message(
                f"A panel can hold at most {MAX_ROLE_BUTTONS} buttons — remove some first.",
                ephemeral=True,
            )
            return
        self.button_roles.extend(new)
        await interaction.response.edit_message(content=self.content(), view=self)

    @discord.ui.button(label="Remove selected", style=discord.ButtonStyle.grey, row=1)
    async def remove_roles(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        picked = {role.id for role in self.roles_select.values}
        if not picked:
            await interaction.response.send_message(
                "Pick roles in the dropdown first.", ephemeral=True
            )
            return
        self.button_roles = [role for role in self.button_roles if role.id not in picked]
        await interaction.response.edit_message(content=self.content(), view=self)

    @discord.ui.button(label="Clear all", style=discord.ButtonStyle.grey, row=1)
    async def clear_roles(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.button_roles = []
        await interaction.response.edit_message(content=self.content(), view=self)

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.grey, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.state.button_roles = list(self.button_roles)
        view = SetupChannelView(self.state, interaction.guild)
        await interaction.response.edit_message(content=view.content(), view=view)

    @discord.ui.button(label="Save & post panels", style=discord.ButtonStyle.green, row=2)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.state.channel is None:
            await interaction.response.send_message(
                "Go back and pick the LFG channel first.", ephemeral=True
            )
            return
        guild = interaction.guild
        button_roles = list(self.button_roles)
        try:
            role_message, lfg_message = await post_panels(self.state.channel, guild, button_roles)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't post in that channel. Check my Send Messages permission there.", ephemeral=True
            )
            return
        # Delete the old panels, possibly in the previous channel.
        old_channel = guild.get_channel(config["channel_id"]) if config["channel_id"] else None
        for channel in {self.state.channel, old_channel}:
            if channel is None:
                continue
            for key in ("panel_message_id", "lfg_message_id"):
                message_id = config[key]
                if message_id is None:
                    continue
                try:
                    await (await channel.fetch_message(message_id)).delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
        # The status embed belongs below the panels, so it's reposted along with them.
        if old_channel is not None and config["status_message_id"]:
            try:
                await (await old_channel.fetch_message(config["status_message_id"])).delete()
            except discord.HTTPException:
                pass
        config["status_message_id"] = None
        config["guild_id"] = guild.id
        config["channel_id"] = self.state.channel.id
        config["ping_roles"] = [role.id for role in self.state.ping_roles]
        config["admin_role_id"] = self.state.admin_role.id if self.state.admin_role else None
        config["button_roles"] = [role.id for role in button_roles]
        config["panel_message_id"] = role_message.id
        config["lfg_message_id"] = lfg_message.id
        save()
        await interaction.response.edit_message(
            content=f"✅ Saved! Panels posted in {self.state.channel.mention}.", view=None
        )
        # Post the status embed last so the channel stays panels → status.
        await refresh_status(self.state.channel)
