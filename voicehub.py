"""Voice hub: joining the hub channel creates a personal channel with an owner panel."""

from __future__ import annotations

import discord

from config import config, save
from permissions import AdminOnlyView

PANEL_TEXT = "**Voice controls** — only the channel owner can use these buttons."


def build_control_view(entry: dict) -> "VoiceControlView":
    """A fresh view so the panel shows the channel's current state."""
    view = VoiceControlView()
    view.lock.label = "Unlock" if entry.get("locked") else "Lock"
    view.hide.label = "Unhide" if entry.get("hidden") else "Hide"
    return view


async def _log(channel: discord.VoiceChannel, entry: dict, text: str) -> None:
    """Append an event to the channel's log embed, editing the same message each time."""
    log_channel = (
        channel.guild.get_channel(config["vc_log_channel_id"])
        if config["vc_log_channel_id"]
        else None
    )
    if log_channel is None:
        return
    events = entry.setdefault("log", [])
    events.append(text)
    embed = discord.Embed(
        title=f"Voice channel — {channel.name}"[:256],
        description="\n".join(events)[-4096:],
        color=discord.Color.blurple(),
    )
    message = None
    if entry.get("log_message_id"):
        try:
            message = await log_channel.fetch_message(entry["log_message_id"])
        except discord.HTTPException:
            message = None
    try:
        if message is None:
            message = await log_channel.send(embed=embed)
            entry["log_message_id"] = message.id
        else:
            await message.edit(embed=embed)
    except discord.HTTPException as e:
        print(f"Couldn't write to the voice log channel: {e}")
    save()


def _restrict_role(guild: discord.Guild) -> discord.Role | None:
    """The role Lock and Hide deny, or None if it was deleted or never set."""
    if not config["vc_restrict_role_id"]:
        return None
    return guild.get_role(config["vc_restrict_role_id"])


async def _control_target(
    interaction: discord.Interaction,
) -> tuple[discord.VoiceChannel, dict] | None:
    """The voice channel and its entry, if the caller owns it."""
    channel = interaction.channel
    entry = config["voice_channels"].get(str(channel.id)) if channel is not None else None
    if not isinstance(channel, discord.VoiceChannel) or entry is None:
        await interaction.response.send_message(
            "This control panel is no longer active.", ephemeral=True
        )
        return None
    if interaction.user.id != entry["owner_id"]:
        await interaction.response.send_message(
            "Only the channel owner can use these buttons.", ephemeral=True
        )
        return None
    return channel, entry


class SetLimitModal(discord.ui.Modal, title="Set user limit"):
    limit_input = discord.ui.TextInput(
        label="User limit",
        placeholder="0 removes the limit",
        max_length=2,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        target = await _control_target(interaction)
        if target is None:
            return
        channel, entry = target
        try:
            limit = int(str(self.limit_input.value).strip())
        except ValueError:
            limit = -1
        if not 0 <= limit <= 99:
            await interaction.response.send_message(
                "Enter a number from 0 to 99 (0 removes the limit).", ephemeral=True
            )
            return
        await interaction.response.defer()
        try:
            await channel.edit(user_limit=limit)
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"Couldn't set the limit: {e}", ephemeral=True
            )
            return
        await _log(
            channel,
            entry,
            f"{interaction.user} ({interaction.user.id}) "
            + (
                f"removed the user limit of {channel.mention}."
                if limit == 0
                else f"set the user limit of {channel.mention} to {limit}."
            ),
        )
        await interaction.followup.send(
            "User limit removed." if limit == 0 else f"User limit set to {limit}.",
            ephemeral=True,
        )


class VoiceControlView(discord.ui.View):
    """Owner-only controls posted in every created voice channel's text chat."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Lock", style=discord.ButtonStyle.grey, custom_id="vc:lock")
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        target = await _control_target(interaction)
        if target is None:
            return
        channel, entry = target
        role = _restrict_role(channel.guild)
        if role is None:
            await interaction.response.send_message(
                "The Lock/Hide role was deleted — update VC setup.", ephemeral=True
            )
            return
        await interaction.response.defer()
        entry["locked"] = not entry.get("locked", False)
        overwrite = channel.overwrites_for(role)
        overwrite.connect = False if entry["locked"] else None
        try:
            await channel.set_permissions(role, overwrite=overwrite)
        except discord.HTTPException as e:
            entry["locked"] = not entry["locked"]
            await interaction.followup.send(
                f"Couldn't update the channel: {e}", ephemeral=True
            )
            return
        save()
        await _log(
            channel,
            entry,
            f"{interaction.user} ({interaction.user.id}) "
            f"{'locked' if entry['locked'] else 'unlocked'} {channel.mention}.",
        )
        await interaction.edit_original_response(
            content=PANEL_TEXT, view=build_control_view(entry)
        )

    @discord.ui.button(label="Set limit", style=discord.ButtonStyle.grey, custom_id="vc:limit")
    async def limit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await _control_target(interaction) is None:
            return
        await interaction.response.send_modal(SetLimitModal())

    @discord.ui.button(label="Hide", style=discord.ButtonStyle.grey, custom_id="vc:hide")
    async def hide(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        target = await _control_target(interaction)
        if target is None:
            return
        channel, entry = target
        role = _restrict_role(channel.guild)
        if role is None:
            await interaction.response.send_message(
                "The Lock/Hide role was deleted — update VC setup.", ephemeral=True
            )
            return
        await interaction.response.defer()
        entry["hidden"] = not entry.get("hidden", False)
        overwrite = channel.overwrites_for(role)
        overwrite.view_channel = False if entry["hidden"] else None
        try:
            await channel.set_permissions(role, overwrite=overwrite)
        except discord.HTTPException as e:
            entry["hidden"] = not entry["hidden"]
            await interaction.followup.send(
                f"Couldn't update the channel: {e}", ephemeral=True
            )
            return
        save()
        await _log(
            channel,
            entry,
            f"{interaction.user} ({interaction.user.id}) "
            f"{'hid' if entry['hidden'] else 'unhid'} {channel.mention}.",
        )
        await interaction.edit_original_response(
            content=PANEL_TEXT, view=build_control_view(entry)
        )


async def _create_channel(member: discord.Member, hub: discord.VoiceChannel) -> None:
    guild = member.guild
    overwrites = dict(hub.overwrites)
    overwrites[member] = discord.PermissionOverwrite(view_channel=True, connect=True)
    if guild.me is not None:
        # The bot must keep seeing and managing the channel after Hide / Lock.
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            manage_roles=True,
        )
    try:
        channel = await guild.create_voice_channel(
            name=f"{member.display_name}'s voice chat"[:100],
            category=hub.category,
            overwrites=overwrites,
        )
    except discord.HTTPException as e:
        print(f"Couldn't create a voice channel for {member}: {e}")
        return
    entry = {
        "owner_id": member.id,
        "panel_message_id": None,
        "locked": False,
        "hidden": False,
        "log_message_id": None,
    }
    # Track the channel before moving its owner so the empty-channel cleanup works
    # even if they leave straight away.
    config["voice_channels"][str(channel.id)] = entry
    try:
        panel = await channel.send(PANEL_TEXT, view=build_control_view(entry))
        entry["panel_message_id"] = panel.id
    except discord.HTTPException as e:
        print(f"Couldn't post the control panel in {channel}: {e}")
    save()
    try:
        await member.move_to(channel)
    except discord.HTTPException as e:
        print(f"Couldn't move {member} into their voice channel: {e}")
        if not channel.members:
            config["voice_channels"].pop(str(channel.id), None)
            save()
            try:
                await channel.delete()
            except discord.HTTPException:
                pass
        return
    await _log(
        channel,
        entry,
        f"{member} ({member.id}) created {channel.mention} ({channel.id}).",
    )


async def _cleanup(channel: discord.VoiceChannel, member: discord.Member) -> None:
    """Delete a created channel once its last member leaves."""
    if str(channel.id) not in config["voice_channels"]:
        return
    if any(existing.id != member.id for existing in channel.members):
        return
    config["voice_channels"].pop(str(channel.id), None)
    save()
    try:
        await channel.delete()
    except discord.HTTPException as e:
        print(f"Couldn't delete the empty voice channel {channel}: {e}")


async def handle_voice_state(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    """Create a channel for hub joiners; delete created channels once empty."""
    if member.bot:
        return
    hub_id = config["vc_hub_channel_id"]
    if (
        hub_id
        and after.channel is not None
        and after.channel.id == hub_id
        and before.channel != after.channel
    ):
        hub = member.guild.get_channel(hub_id)
        if isinstance(hub, discord.VoiceChannel):
            await _create_channel(member, hub)
    if (
        before.channel is not None
        and before.channel != after.channel
        and str(before.channel.id) in config["voice_channels"]
    ):
        await _cleanup(before.channel, member)


class VoiceSetupView(AdminOnlyView):
    """Setup page: the hub voice channel and an optional activity log channel."""

    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=300)
        hub_default = (
            [guild.get_channel(config["vc_hub_channel_id"])]
            if config["vc_hub_channel_id"] and guild.get_channel(config["vc_hub_channel_id"])
            else []
        )
        self.hub_select = discord.ui.ChannelSelect(
            placeholder="Voice channel that creates personal channels",
            channel_types=[discord.ChannelType.voice],
            min_values=1,
            max_values=1,
            row=0,
            default_values=hub_default,
        )
        log_default = (
            [guild.get_channel(config["vc_log_channel_id"])]
            if config["vc_log_channel_id"] and guild.get_channel(config["vc_log_channel_id"])
            else []
        )
        self.log_select = discord.ui.ChannelSelect(
            placeholder="Channel for voice logs (optional)",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=1,
            row=1,
            default_values=log_default,
        )
        role_default = (
            [guild.get_role(config["vc_restrict_role_id"])]
            if config["vc_restrict_role_id"] and guild.get_role(config["vc_restrict_role_id"])
            else []
        )
        self.role_select = discord.ui.RoleSelect(
            placeholder="Role that Lock and Hide deny access to",
            min_values=1,
            max_values=1,
            row=2,
            default_values=role_default,
        )
        for select in (self.hub_select, self.log_select, self.role_select):
            select.callback = self._ack
            self.add_item(select)

    async def _ack(self, interaction: discord.Interaction) -> None:
        # Selects are only read when "Save" is pressed — just acknowledge the pick.
        await interaction.response.defer()

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.grey, row=3)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from views import SetupMenuView

        await interaction.response.edit_message(
            content="**Setup** — what do you want to configure?",
            view=SetupMenuView(interaction.guild),
        )

    @discord.ui.button(label="Save voice setup", style=discord.ButtonStyle.green, row=3)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild = interaction.guild
        hubs = self.hub_select.values or self.hub_select.default_values
        if not hubs:
            await interaction.response.send_message(
                "Pick the hub voice channel first.", ephemeral=True
            )
            return
        hub = guild.get_channel(hubs[0].id)
        if not isinstance(hub, discord.VoiceChannel):
            await interaction.response.send_message(
                "That voice channel doesn't exist anymore.", ephemeral=True
            )
            return
        roles = self.role_select.values or self.role_select.default_values
        if not roles:
            await interaction.response.send_message(
                "Pick the role Lock and Hide should deny.", ephemeral=True
            )
            return
        role = guild.get_role(roles[0].id)
        if role is None:
            await interaction.response.send_message(
                "That role doesn't exist anymore.", ephemeral=True
            )
            return
        logs = self.log_select.values or self.log_select.default_values
        log_channel = guild.get_channel(logs[0].id) if logs else None
        config["vc_hub_channel_id"] = hub.id
        config["vc_log_channel_id"] = log_channel.id if log_channel else None
        config["vc_restrict_role_id"] = role.id
        save()
        await interaction.response.edit_message(
            content=f"**Voice setup** — hub: {hub.mention}; "
            f"logs: {log_channel.mention if log_channel else 'off'}; "
            f"Lock/Hide role: {role.mention}.",
            view=self,
        )
