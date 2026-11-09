"""Server tags: /tag create/manage/list/use and the "." trigger in messages."""

from __future__ import annotations

import datetime as dt
import re

import discord
from discord import app_commands

import modmail
import permissions
from config import config, save

NAME_RE = re.compile(r"[a-z0-9_-]{1,32}")
MENTION_RE = re.compile(r"<@&(\d+)>|<@!?(\d+)>")
# Flags after a tag name: "-c <channel id>" and "-u <user id>"; mentions work too.
FLAG_RE = re.compile(r"(?:^|\s)-(c|u)\s+(?:<[#@!&]+)?(\d+)")
TRIGGER_PREFIX = "."
# Fixed audit-log channel: every tag create/edit/delete is posted here. Usage isn't.
TAG_LOG_CHANNEL_ID = 1567214449478336553


async def _log_tag_change(
    bot: discord.Client,
    title: str,
    user: discord.abc.User,
    description: str | None = None,
    *,
    color: discord.Color,
) -> None:
    """Post a tag change to the audit-log channel with who made it and when."""
    channel = bot.get_channel(TAG_LOG_CHANNEL_ID)
    if channel is None:
        return
    embed = discord.Embed(
        title=title[:256],
        description=description[:4096] if description else None,
        color=color,
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    embed.add_field(name="By", value=f"{user.mention} (`{user.id}`)", inline=False)
    try:
        await channel.send(embed=embed)
    except discord.HTTPException as e:
        print(f"Couldn't write to the tag log channel: {e}")


def normalize_name(name: str) -> str | None:
    """Lowercase a tag name, or return None if it isn't usable."""
    name = name.strip().casefold()
    return name if NAME_RE.fullmatch(name) else None


def _can_manage(user: discord.abc.User, entry: dict) -> bool:
    """Tag creators manage their own tags; admins manage all of them."""
    return user.id == entry.get("author_id") or permissions.is_admin(user)


def _is_moderator(user: discord.abc.User) -> bool:
    return isinstance(user, discord.Member) and modmail.is_moderator(user)


def build_tag_embed(name: str, entry: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f".{name}"[:256],
        description=entry["content"][:4096],
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Created by", value=f"<@{entry['author_id']}>", inline=True)
    embed.add_field(
        name="Created",
        value=discord.utils.format_dt(dt.datetime.fromisoformat(entry["created"]), "f"),
        inline=True,
    )
    embed.add_field(name="Uses", value=str(entry.get("uses", 0)), inline=True)
    embed.add_field(
        name="Pings mentions",
        value="Yes" if entry.get("ping_mentions") else "No",
        inline=True,
    )
    return embed


async def tag_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[discord.app_commands.Choice[str]]:
    current = current.casefold()
    names = [name for name in config["tags"] if current in name]
    names.sort(key=lambda name: (not name.startswith(current), name))
    return [discord.app_commands.Choice(name=name, value=name) for name in names[:25]]


def _mentioned_ids(content: str) -> tuple[list[int], list[int]]:
    """Role and user IDs mentioned in a tag's content."""
    role_ids: list[int] = []
    user_ids: list[int] = []
    for match in MENTION_RE.finditer(content):
        if match.group(1) is not None:
            role_ids.append(int(match.group(1)))
        else:
            user_ids.append(int(match.group(2)))
    return list(dict.fromkeys(role_ids)), list(dict.fromkeys(user_ids))


def _parse_flags(text: str) -> list[tuple[str, int]]:
    """Pull "-c <channel id>" and "-u <user id>" flags out of the text after a name."""
    return [(match.group(1), int(match.group(2))) for match in FLAG_RE.finditer(text)]


async def _post_tag(
    channel, name: str, entry: dict, ping_user_id: int | None = None
) -> bool:
    """Post a tag's content; its mentions ping only if the tag says so.

    ping_user_id prepends a mention of that user and lets it ping regardless of the
    tag's ping setting — the poster asked for that ping explicitly.
    """
    content = entry["content"]
    allowed = discord.AllowedMentions.none()
    if entry.get("ping_mentions"):
        role_ids, user_ids = _mentioned_ids(entry["content"])
        if ping_user_id is not None and ping_user_id not in user_ids:
            user_ids.append(ping_user_id)
        # everyone=False: without it AllowedMentions would also allow @everyone/@here.
        allowed = discord.AllowedMentions(
            everyone=False,
            roles=[discord.Object(id=role_id) for role_id in role_ids],
            users=[discord.Object(id=user_id) for user_id in user_ids],
        )
    elif ping_user_id is not None:
        allowed = discord.AllowedMentions(
            everyone=False, users=[discord.Object(id=ping_user_id)]
        )
    if ping_user_id is not None:
        content = f"<@{ping_user_id}> {content}"
    try:
        await channel.send(content, allowed_mentions=allowed)
    except discord.HTTPException as e:
        print(f"Couldn't post tag .{name}: {e}")
        return False
    entry["uses"] = entry.get("uses", 0) + 1
    save()
    return True


async def handle_message(message: discord.Message) -> bool:
    """Post a tag when a message starts with "." followed by an existing name.

    Flags after the name: "-c <channel id>" posts the tag there instead of the
    current channel (moderators only), and "-u <user id>" pings that user.
    """
    if message.guild is None or message.author.bot:
        return False
    content = message.content.lstrip()
    if not content.startswith(TRIGGER_PREFIX):
        return False
    parts = content[1:].split(maxsplit=1)
    name = normalize_name(parts[0]) if parts else None
    if name is None:
        return False
    entry = config["tags"].get(name)
    if entry is None:
        return False
    flags = _parse_flags(parts[1]) if len(parts) > 1 else []
    channel_id = next((value for flag, value in flags if flag == "c"), None)
    ping_user_id = next((value for flag, value in flags if flag == "u"), None)
    channel = message.channel
    if channel_id is not None:
        if not _is_moderator(message.author):
            await message.channel.send(
                "Only moderators can post a tag in another channel."
            )
            return True
        channel = message.guild.get_channel_or_thread(channel_id)
        if channel is None:
            try:
                channel = await message.guild.fetch_channel(channel_id)
            except discord.HTTPException:
                channel = None
        if not hasattr(channel, "send"):
            await message.channel.send(
                f"I can't post tags in a channel with ID `{channel_id}` — "
                "pick a text channel or thread."
            )
            return True
    posted = await _post_tag(channel, name, entry, ping_user_id=ping_user_id)
    if not posted and channel is not message.channel:
        await message.channel.send(
            f"I couldn't post `.{name}` in {channel.mention} — check my permissions there."
        )
    return posted


tag_group = app_commands.Group(name="tag", description="Create, manage and list tags")


@tag_group.command(name="create", description="Create a tag")
@app_commands.describe(name="Name used to call the tag (for example: test for .test)")
async def tag_create(interaction: discord.Interaction, name: str) -> None:
    if not _is_moderator(interaction.user):
        await interaction.response.send_message(
            "Only moderators can create tags.", ephemeral=True
        )
        return
    normalized = normalize_name(name)
    if normalized is None:
        await interaction.response.send_message(
            "Tag names must be 1-32 characters: letters, numbers, `-` or `_`.",
            ephemeral=True,
        )
        return
    if normalized in config["tags"]:
        await interaction.response.send_message(
            f"`.{normalized}` already exists.", ephemeral=True
        )
        return
    await interaction.response.send_modal(TagContentModal(normalized))


@tag_group.command(name="manage", description="View and manage a tag")
@app_commands.describe(
    name="Tag to manage",
    ephemeral="Only you can see the tag details (default: yes)",
)
@app_commands.autocomplete(name=tag_autocomplete)
async def tag_manage(
    interaction: discord.Interaction, name: str, ephemeral: bool = True
) -> None:
    normalized = normalize_name(name)
    entry = config["tags"].get(normalized) if normalized else None
    if entry is None:
        await interaction.response.send_message(
            "There's no tag with that name. Use `/tag list` to see the existing tags.",
            ephemeral=True,
        )
        return
    if not _can_manage(interaction.user, entry):
        await interaction.response.send_message(
            "Only the tag's creator or an admin can manage it.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        embed=build_tag_embed(normalized, entry),
        view=TagManageView(normalized),
        ephemeral=ephemeral,
    )


@tag_group.command(name="list", description="List all tags")
@app_commands.describe(ephemeral="Only you can see the tag list (default: yes)")
async def tag_list(interaction: discord.Interaction, ephemeral: bool = True) -> None:
    if not config["tags"]:
        await interaction.response.send_message(
            "No tags exist yet. Create one with `/tag create`.", ephemeral=ephemeral
        )
        return
    lines: list[str] = []
    length = 0
    for name in sorted(config["tags"]):
        line = f"**.{name}** — {config['tags'][name].get('uses', 0)} uses"
        if length + len(line) + 1 > 4000:
            lines.append(f"…and {len(config['tags']) - len(lines)} more.")
            break
        lines.append(line)
        length += len(line) + 1
    embed = discord.Embed(
        title=f"Tags ({len(config['tags'])})",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=ephemeral)


@tag_group.command(name="use", description="Post a tag")
@app_commands.describe(
    name="Tag to post",
    channel="Channel to post it in (default: the current channel)",
    mention="User to ping with the tag",
)
@app_commands.autocomplete(name=tag_autocomplete)
async def tag_use(
    interaction: discord.Interaction,
    name: str,
    channel: discord.TextChannel | None = None,
    mention: discord.User | None = None,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "Tags only work in the server.", ephemeral=True
        )
        return
    normalized = normalize_name(name)
    entry = config["tags"].get(normalized) if normalized else None
    if entry is None:
        await interaction.response.send_message(
            "There's no tag with that name. Use `/tag list` to see the existing tags.",
            ephemeral=True,
        )
        return
    if channel is not None and not _is_moderator(interaction.user):
        await interaction.response.send_message(
            "Only moderators can post a tag in another channel.", ephemeral=True
        )
        return
    target = channel if channel is not None else interaction.channel
    await interaction.response.defer(ephemeral=True)
    posted = await _post_tag(
        target, normalized, entry, ping_user_id=mention.id if mention else None
    )
    if posted:
        await interaction.followup.send(
            f"Posted **.{normalized}** in {target.mention}.", ephemeral=True
        )
    else:
        await interaction.followup.send(
            "I couldn't post the tag — check my permissions in that channel.",
            ephemeral=True,
        )


class TagContentModal(discord.ui.Modal):
    """Large text input for creating a tag or editing its content."""

    def __init__(
        self,
        name: str,
        view: TagManageView | None = None,
        existing: str | None = None,
    ):
        super().__init__(title=(f"Edit tag .{name}" if view else f"Create tag .{name}")[:45])
        self.tag_name = name
        self.manage_view = view
        self.content_input = discord.ui.TextInput(
            label="Content",
            style=discord.TextStyle.paragraph,
            placeholder="What should the bot reply with?",
            max_length=2000,
            default=existing or "",
        )
        self.add_item(self.content_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text = str(self.content_input.value).strip()
        if not text:
            await interaction.response.send_message(
                "The tag content can't be empty.", ephemeral=True
            )
            return
        if self.manage_view is None:
            if self.tag_name in config["tags"]:
                await interaction.response.send_message(
                    f"`.{self.tag_name}` already exists.", ephemeral=True
                )
                return
            config["tags"][self.tag_name] = {
                "content": text,
                "author_id": interaction.user.id,
                "created": dt.datetime.now(dt.timezone.utc).isoformat(),
                "uses": 0,
                "ping_mentions": False,
            }
            save()
            await _log_tag_change(
                interaction.client,
                f"Tag created — .{self.tag_name}",
                interaction.user,
                text,
                color=discord.Color.green(),
            )
            role_ids, user_ids = _mentioned_ids(text)
            if role_ids or user_ids:
                await interaction.response.send_message(
                    f"Tag **.{self.tag_name}** created. Should its mentions ping "
                    "when the tag is used?",
                    view=PingMentionsView(self.tag_name),
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                f"Tag **.{self.tag_name}** created. Call it with `.{self.tag_name}`.",
                ephemeral=True,
            )
            return
        entry = config["tags"].get(self.tag_name)
        if entry is None:
            await interaction.response.send_message(
                "That tag no longer exists.", ephemeral=True
            )
            return
        if not _can_manage(interaction.user, entry):
            await interaction.response.send_message(
                "Only the tag's creator or an admin can edit it.", ephemeral=True
            )
            return
        entry["content"] = text
        save()
        await _log_tag_change(
            interaction.client,
            f"Tag edited — .{self.tag_name}",
            interaction.user,
            text,
            color=discord.Color.orange(),
        )
        await interaction.response.edit_message(
            embed=build_tag_embed(self.tag_name, entry), view=self.manage_view
        )


class PingMentionsView(discord.ui.View):
    """Create-time question: should the tag's mentions ping when it's used?"""

    def __init__(self, name: str):
        super().__init__(timeout=300)
        self.name = name

    async def _set(self, interaction: discord.Interaction, ping: bool) -> None:
        entry = config["tags"].get(self.name)
        if entry is None:
            await interaction.response.edit_message(
                content=f"`.{self.name}` no longer exists.", view=None
            )
            self.stop()
            return
        entry["ping_mentions"] = ping
        save()
        await interaction.response.edit_message(
            content=(
                f"Tag **.{self.name}** will ping its mentions."
                if ping
                else f"Tag **.{self.name}** won't ping its mentions."
            ),
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Ping", style=discord.ButtonStyle.green)
    async def ping(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._set(interaction, True)

    @discord.ui.button(label="Don't ping", style=discord.ButtonStyle.grey)
    async def no_ping(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._set(interaction, False)


class RenameTagModal(discord.ui.Modal, title="Rename tag"):
    def __init__(self, view: TagManageView):
        super().__init__()
        self.manage_view = view
        self.name_input = discord.ui.TextInput(
            label="New name", max_length=32, default=view.name
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        old_name = self.manage_view.name
        entry = config["tags"].get(old_name)
        if entry is None:
            await interaction.response.send_message(
                "That tag no longer exists.", ephemeral=True
            )
            return
        if not _can_manage(interaction.user, entry):
            await interaction.response.send_message(
                "Only the tag's creator or an admin can rename it.", ephemeral=True
            )
            return
        new_name = normalize_name(self.name_input.value)
        if new_name is None:
            await interaction.response.send_message(
                "Tag names must be 1-32 characters: letters, numbers, `-` or `_`.",
                ephemeral=True,
            )
            return
        if new_name != old_name:
            if new_name in config["tags"]:
                await interaction.response.send_message(
                    f"`.{new_name}` already exists.", ephemeral=True
                )
                return
            del config["tags"][old_name]
            config["tags"][new_name] = entry
            save()
            await _log_tag_change(
                interaction.client,
                f"Tag renamed — .{old_name} → .{new_name}",
                interaction.user,
                color=discord.Color.orange(),
            )
        self.manage_view.name = new_name
        await interaction.response.edit_message(
            embed=build_tag_embed(new_name, entry), view=self.manage_view
        )


class TagManageView(discord.ui.View):
    def __init__(self, name: str):
        super().__init__(timeout=300)
        self.name = name
        self.toggle_ping.label = (
            "Ping mentions: on"
            if config["tags"].get(name, {}).get("ping_mentions")
            else "Ping mentions: off"
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        entry = config["tags"].get(self.name)
        if entry is None:
            await interaction.response.send_message(
                "That tag no longer exists.", ephemeral=True
            )
            return False
        if not _can_manage(interaction.user, entry):
            await interaction.response.send_message(
                "Only the tag's creator or an admin can manage it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Ping mentions: off", style=discord.ButtonStyle.grey)
    async def toggle_ping(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        entry = config["tags"][self.name]
        entry["ping_mentions"] = not entry.get("ping_mentions")
        save()
        button.label = (
            "Ping mentions: on" if entry["ping_mentions"] else "Ping mentions: off"
        )
        await interaction.response.edit_message(
            embed=build_tag_embed(self.name, entry), view=self
        )

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.blurple)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        entry = config["tags"][self.name]
        await interaction.response.send_modal(
            TagContentModal(self.name, view=self, existing=entry["content"])
        )

    @discord.ui.button(label="Rename", style=discord.ButtonStyle.grey)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(RenameTagModal(self))

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.red)
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content=f"Remove **.{self.name}**? This can't be undone.",
            embed=None,
            view=ConfirmRemoveView(self),
        )


class ConfirmRemoveView(discord.ui.View):
    def __init__(self, manage_view: TagManageView):
        super().__init__(timeout=120)
        self.manage_view = manage_view
        self.name = manage_view.name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        entry = config["tags"].get(self.name)
        if entry is None:
            await interaction.response.edit_message(
                content=f"`.{self.name}` no longer exists.", embed=None, view=None
            )
            return False
        if not _can_manage(interaction.user, entry):
            await interaction.response.send_message(
                "Only the tag's creator or an admin can remove it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.red)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        entry = config["tags"].pop(self.name, None)
        save()
        await _log_tag_change(
            interaction.client,
            f"Tag deleted — .{self.name}",
            interaction.user,
            entry.get("content") if entry else None,
            color=discord.Color.red(),
        )
        await interaction.response.edit_message(
            content=f"Tag **.{self.name}** removed.", embed=None, view=None
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        entry = config["tags"].get(self.name)
        if entry is None:
            await interaction.response.edit_message(
                content=f"`.{self.name}` no longer exists.", embed=None, view=None
            )
            return
        await interaction.response.edit_message(
            content=None,
            embed=build_tag_embed(self.name, entry),
            view=self.manage_view,
        )


def register(bot) -> None:
    """Add the /tag group to the command tree once."""
    if bot.tree.get_command("tag") is None:
        bot.tree.add_command(tag_group)
