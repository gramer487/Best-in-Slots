"""Modmail: relay DMs between server members and the moderator role.

Users open modmail with the panel button or by DMing the bot. Each user gets one
private channel in a staff-only category. Moderators answer with `&reply <text>`
(or `&r <text>`) and close with `&close` or the Close button, which logs a transcript
to a forum channel.
"""

from __future__ import annotations

import datetime as dt
import re

import discord

from config import config, save

CATEGORY_NAME = "Modmail"
REPLY_PREFIX = "Reply from Moderator: "
_CONFIRM_DM = "✅ Your message has been successfully forwarded to the staff team."


def _channel_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-")
    return cleaned[:100] or "user"


async def _ensure_category(guild: discord.Guild) -> discord.CategoryChannel | None:
    """Get (or create) the staff-only category and keep its permissions in sync."""
    role = guild.get_role(config["modmail_mod_role_id"]) if config["modmail_mod_role_id"] else None
    if role is None:
        return None
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        role: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
            manage_messages=True,
        ),
    }
    category = (
        guild.get_channel(config["modmail_category_id"]) if config["modmail_category_id"] else None
    )
    if isinstance(category, discord.CategoryChannel):
        await category.edit(overwrites=overwrites)
        return category
    category = await guild.create_category(CATEGORY_NAME, overwrites=overwrites)
    config["modmail_category_id"] = category.id
    save()
    return category


async def ensure_modmail_channel(
    guild: discord.Guild, member: discord.Member
) -> tuple[discord.TextChannel | None, bool]:
    """Return (channel, created) for this member's modmail channel."""
    existing = config["modmails"].get(str(member.id))
    if existing:
        channel = guild.get_channel(existing)
        if channel is not None:
            return channel, False
        config["modmails"].pop(str(member.id), None)
        save()
    try:
        category = await _ensure_category(guild)
        if category is None:
            return None, False
        channel = await category.create_text_channel(
            name=_channel_name(member.name),
            topic=f"Modmail with {member.name} ({member.id})",
        )
    except discord.Forbidden:
        return None, False
    config["modmails"][str(member.id)] = channel.id
    save()
    return channel, True


async def _announce_new_channel(channel: discord.TextChannel, member: discord.Member) -> None:
    """First message in the channel: who they are, past mails, and the commands."""
    guild = channel.guild
    embed = discord.Embed(
        title=f"New modmail from {member.name}",
        description=(
            "Reply with `&reply <message>` (or `&r`) — close with `&close` or the button."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="User", value=f"{member.name} ({member.id})", inline=True)
    if member.joined_at is not None:
        joined = int(member.joined_at.timestamp())
        embed.add_field(name="Joined", value=f"<t:{joined}:F> (<t:{joined}:R>)", inline=True)
    created = int(member.created_at.timestamp())
    embed.add_field(name="Account created", value=f"<t:{created}:F> (<t:{created}:R>)", inline=True)
    top_roles = sorted(
        (r for r in member.roles if r != guild.default_role),
        key=lambda r: r.position,
        reverse=True,
    )[:15]
    embed.add_field(
        name="Top roles",
        value=", ".join(r.name for r in top_roles)[:1024] or "None",
        inline=False,
    )
    history = config["modmail_history"].get(str(member.id), [])
    if history:
        links = "\n".join(
            f"[{entry['title']}](https://discord.com/channels/{guild.id}/{entry['thread']})"
            for entry in history
        )
        embed.add_field(name="Past modmails", value=links[:1024], inline=False)
    ping_roles = [r for rid in config["modmail_ping_roles"] if (r := guild.get_role(rid))]
    await channel.send(
        content=" ".join(r.mention for r in ping_roles) or None,
        embed=embed,
        view=ModmailCloseView(),
        allowed_mentions=discord.AllowedMentions(roles=ping_roles),
    )


def _user_embed(author: discord.abc.User, text: str) -> discord.Embed:
    return discord.Embed(
        title=f"From {author.name}", description=text[:4096], color=discord.Color.blue()
    )


def _reply_embed(moderator_name: str, text: str) -> discord.Embed:
    return discord.Embed(
        title=f"{REPLY_PREFIX}{moderator_name}",
        description=text[:4096],
        color=discord.Color.green(),
    )


def _message_text(message: discord.Message) -> str:
    parts = [message.content.strip(), *(a.url for a in message.attachments)]
    return "\n".join(p for p in parts if p) or "[no text]"


class ModmailPanelView(discord.ui.View):
    """Persistent 'contact a moderator' button."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Contact a Moderator",
        style=discord.ButtonStyle.blurple,
        emoji="📨",
        custom_id="open_modmail",
    )
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Ask first so a stray click doesn't open a mail.
        await interaction.response.send_message(
            "Do you want to open a modmail with the staff team?",
            view=ModmailConfirmView(),
            ephemeral=True,
        )


class ModmailConfirmView(discord.ui.View):
    """Confirm/Cancel prompt shown after the panel button is pressed."""

    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Channel creation can take a few seconds — acknowledge before doing the work.
        await interaction.response.defer()
        guild = interaction.guild
        # Guild interactions carry the full Member regardless of the member cache.
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if guild is None or member is None:
            await interaction.edit_original_response(
                content="You need to be a member of the server to contact the moderators.",
                view=None,
            )
            return
        channel, created = await ensure_modmail_channel(guild, member)
        if channel is None:
            await interaction.edit_original_response(
                content="Modmail isn't configured yet. Ask an admin to run `/setup`.",
                view=None,
            )
            return
        if created:
            await _announce_new_channel(channel, member)
        try:
            await member.send(_CONFIRM_DM)
        except discord.HTTPException:
            pass
        await interaction.edit_original_response(
            content="✅ A moderator will be with you shortly — check your DMs.", view=None
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="No problem — nothing was opened.", view=None
        )


async def handle_dm(message: discord.Message, guild: discord.Guild | None) -> None:
    """Forward a user's DM into their modmail channel, opening one if needed."""
    if guild is None:
        return
    member = guild.get_member(message.author.id)
    if member is None:
        try:
            member = await guild.fetch_member(message.author.id)
        except discord.NotFound:
            member = None
        except discord.HTTPException:
            return  # API hiccup — don't claim they aren't a member
    if member is None:
        try:
            await message.channel.send(
                "You need to be a member of the server to contact the moderators."
            )
        except discord.HTTPException:
            pass
        return
    channel, created = await ensure_modmail_channel(guild, member)
    if channel is None:
        try:
            await message.channel.send("Modmail isn't configured yet. Ask an admin to run `/setup`.")
        except discord.HTTPException:
            pass
        return
    if created:
        await _announce_new_channel(channel, member)
    await channel.send(embed=_user_embed(member, _message_text(message)))
    if created:
        try:
            await message.channel.send(_CONFIRM_DM)
        except discord.HTTPException:
            pass


def _is_moderator(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    role = member.guild.get_role(config["modmail_mod_role_id"]) if config["modmail_mod_role_id"] else None
    return role is not None and role in member.roles


class ModmailCloseView(discord.ui.View):
    """Persistent Close button shown in every modmail channel."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close", style=discord.ButtonStyle.red, emoji="🗑️", custom_id="close_modmail"
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        user_id = next(
            (uid for uid, cid in config["modmails"].items() if cid == interaction.channel_id),
            None,
        )
        if user_id is None:
            await interaction.response.send_message(
                "This isn't an active modmail.", ephemeral=True
            )
            return
        if not _is_moderator(interaction.user):
            await interaction.response.send_message(
                "Only moderators can close this.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        await _close_modmail(interaction.channel, user_id)


async def handle_staff(message: discord.Message) -> None:
    """Handle `&reply`/`&r` and `&close` inside a modmail channel."""
    user_id = next(
        (uid for uid, cid in config["modmails"].items() if cid == message.channel.id), None
    )
    if user_id is None or not _is_moderator(message.author):
        return
    content = message.content.strip()
    lower = content.lower()
    if lower == "&close":
        await _close_modmail(message.channel, user_id)
        return
    if lower.startswith("&reply "):
        reply = content[7:].strip()
    elif lower.startswith("&r "):
        reply = content[3:].strip()
    else:
        return
    if not reply:
        return
    target = message.guild.get_member(int(user_id))
    if target is None:
        try:
            target = await message.guild.fetch_member(int(user_id))
        except discord.HTTPException:
            target = None
    embed = _reply_embed(message.author.name, reply)
    delivered = False
    if target is not None:
        try:
            await target.send(embed=embed)
            delivered = True
        except discord.HTTPException:
            pass
    await message.channel.send(embed=embed)
    if not delivered:
        await message.channel.send(
            "⚠️ Couldn't DM the user — they may have DMs closed or left the server."
        )
    try:
        await message.delete()  # keep the channel clean — the embed is the record
    except discord.HTTPException:
        pass


async def _build_transcript(channel: discord.TextChannel) -> str:
    lines = []
    async for msg in channel.history(limit=1000, oldest_first=True):
        if msg.author.bot and msg.embeds:
            embed = msg.embeds[0]
            title = embed.title or ""
            text = embed.description or ""
            if title.startswith("From "):
                lines.append(f"{title[5:]}: {text}")
            elif title.startswith(REPLY_PREFIX):
                lines.append(f"{title[len(REPLY_PREFIX):]}: {text}")
        elif not msg.author.bot:
            content = msg.content.strip()
            if content.startswith("&"):  # staff commands, already echoed as embeds
                continue
            lines.append(f"{msg.author.name}: {content}")
    return "\n".join(lines) or "(no messages)"


def _chunks(text: str, size: int = 1900) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or ["(no messages)"]


async def _close_modmail(channel: discord.TextChannel, user_id: str) -> None:
    guild = channel.guild
    member = guild.get_member(int(user_id))
    if member is None:
        try:
            member = await guild.fetch_member(int(user_id))
        except discord.HTTPException:
            member = None
    user_name = member.name if member is not None else channel.name
    transcript = await _build_transcript(channel)

    forum = (
        guild.get_channel(config["modmail_log_channel_id"])
        if config["modmail_log_channel_id"]
        else None
    )
    if isinstance(forum, discord.ForumChannel):
        chunks = _chunks(transcript)
        title = f"{user_name} — {dt.datetime.now():%Y-%m-%d}"
        try:
            thread = await forum.create_thread(
                name=title[:100],
                content=chunks[0],
                allowed_mentions=discord.AllowedMentions.none(),
            )
            # Link this transcript from the user's next modmail info card.
            config["modmail_history"].setdefault(str(user_id), []).append(
                {"thread": thread.thread.id, "title": title}
            )
            save()
            for chunk in chunks[1:]:
                await thread.thread.send(chunk, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            # Keep the channel so the conversation isn't lost when logging fails.
            await channel.send(f"⚠️ Couldn't save the transcript to the log forum: {e}")
            return

    config["modmails"].pop(str(user_id), None)
    save()
    if member is not None:
        try:
            await member.send("Your modmail has been closed. You can message here again anytime.")
        except discord.HTTPException:
            pass
    await channel.delete()


class ModmailSetupView(discord.ui.View):
    """Setup page: panel channel, ping roles, moderator role and log forum."""

    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=300)
        channel_default = (
            [guild.get_channel(config["modmail_embed_channel_id"])]
            if config["modmail_embed_channel_id"]
            and guild.get_channel(config["modmail_embed_channel_id"])
            else []
        )
        self.embed_select = discord.ui.ChannelSelect(
            placeholder="Channel for the contact panel (optional)",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=1,
            row=0,
            default_values=channel_default,
        )
        ping_default = [
            guild.get_role(rid) for rid in config["modmail_ping_roles"] if guild.get_role(rid)
        ]
        self.ping_select = discord.ui.RoleSelect(
            placeholder="Roles pinged for new modmail",
            min_values=0,
            max_values=10,
            row=1,
            default_values=ping_default,
        )
        mod_default = (
            [guild.get_role(config["modmail_mod_role_id"])]
            if config["modmail_mod_role_id"]
            and guild.get_role(config["modmail_mod_role_id"])
            else []
        )
        self.mod_select = discord.ui.RoleSelect(
            placeholder="Moderator role (sees the modmail category)",
            min_values=0,
            max_values=1,
            row=2,
            default_values=mod_default,
        )
        log_default = (
            [guild.get_channel(config["modmail_log_channel_id"])]
            if config["modmail_log_channel_id"]
            and guild.get_channel(config["modmail_log_channel_id"])
            else []
        )
        self.log_select = discord.ui.ChannelSelect(
            placeholder="Modmail log forum (optional)",
            channel_types=[discord.ChannelType.forum],
            min_values=0,
            max_values=1,
            row=3,
            default_values=log_default,
        )
        for select in (self.embed_select, self.ping_select, self.mod_select, self.log_select):
            select.callback = self._ack
            self.add_item(select)

    async def _ack(self, interaction: discord.Interaction) -> None:
        # Selects are only read when "Save" is pressed — just acknowledge the pick.
        await interaction.response.defer()

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.grey, row=4)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from views import SetupMenuView

        await interaction.response.edit_message(
            content="**Setup** — what do you want to configure?",
            view=SetupMenuView(interaction.guild),
        )

    @discord.ui.button(label="Save modmail setup", style=discord.ButtonStyle.green, row=4)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild = interaction.guild
        # Untouched selects return empty values — fall back to the shown defaults.
        mod_roles = self.mod_select.values or self.mod_select.default_values
        if not mod_roles:
            await interaction.response.send_message(
                "Pick the moderator role first.", ephemeral=True
            )
            return
        mod_role = guild.get_role(mod_roles[0].id)
        if mod_role is None:
            await interaction.response.send_message("That role doesn't exist anymore.", ephemeral=True)
            return
        embed_channels = self.embed_select.values or self.embed_select.default_values
        ping_roles = self.ping_select.values or self.ping_select.default_values
        log_channels = self.log_select.values or self.log_select.default_values

        # Category creation + panel posting take several seconds — acknowledge first.
        await interaction.response.defer()
        old_panel_id = config["modmail_panel_message_id"]
        old_panel_channel = (
            guild.get_channel(config["modmail_embed_channel_id"])
            if config["modmail_embed_channel_id"]
            else None
        )

        config["modmail_mod_role_id"] = mod_role.id
        config["modmail_ping_roles"] = [role.id for role in ping_roles]
        config["modmail_embed_channel_id"] = embed_channels[0].id if embed_channels else None
        config["modmail_log_channel_id"] = log_channels[0].id if log_channels else None
        save()

        try:
            await _ensure_category(guild)
        except discord.Forbidden:
            await interaction.edit_original_response(
                content="I can't create the modmail category — I need the Manage Channels permission."
            )
            return

        panel_channel = guild.get_channel(embed_channels[0].id) if embed_channels else None
        if old_panel_id:
            for channel in {old_panel_channel, panel_channel} - {None}:
                try:
                    await (await channel.fetch_message(old_panel_id)).delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
        if panel_channel is not None:
            try:
                message = await panel_channel.send(
                    embed=discord.Embed(
                        title="Contact the Moderators",
                        description="Click the button below to get in touch with a moderator.",
                        color=discord.Color.blurple(),
                    ),
                    view=ModmailPanelView(),
                )
            except discord.Forbidden:
                await interaction.edit_original_response(
                    content="I can't post in that channel — check my Send Messages permission there."
                )
                return
            config["modmail_panel_message_id"] = message.id
            save()

        where = (
            f" Panel posted in {panel_channel.mention}."
            if panel_channel is not None
            else " No panel channel selected — users can still DM the bot."
        )
        await interaction.edit_original_response(
            content=f"✅ Modmail saved!{where} Category **{CATEGORY_NAME}** is visible to {mod_role.mention} only.",
            view=None,
        )
