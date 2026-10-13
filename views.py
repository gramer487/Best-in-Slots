"""UI components: role panel, LFG panel, LFG form and the /setup pages."""

from __future__ import annotations

import datetime as dt

import discord

from config import config, save

PANEL_INTRO = "Pick a role to ping, how many players you need and for how long — then hit **Post LFG**.\n\n"

# Hardcoded per-role player limits: this role allows up to 5, everything else 3.
FIVE_PLAYER_ROLE_ID = 1559600840950423632


def _summary(role, count, duration, note) -> str:
    return (
        f"**Role to ping:** {role.name if role else '❌ not set'}\n"
        f"**Players needed:** {count if count else '❌ not set'}\n"
        f"**Duration:** {duration or '❌ not set'}\n"
        f"**Note:** {note or '—'}"
    )


class RolePanelView(discord.ui.View):
    """Embed 1: three buttons that toggle self-assignable roles."""

    def __init__(self, guild: discord.Guild, roles: list[discord.Role | None] | None = None):
        super().__init__(timeout=None)
        if roles is None:
            roles = [guild.get_role(rid) if rid else None for rid in config["button_roles"]]
        for i in range(3):
            button = self.children[i]
            role = roles[i]
            if role is None:
                button.label = "—"
                button.disabled = True
            else:
                button.label = role.name

    @discord.ui.button(label="Role 1", style=discord.ButtonStyle.blurple, custom_id="role_button_0")
    async def role_button_0(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._toggle(interaction, 0)

    @discord.ui.button(label="Role 2", style=discord.ButtonStyle.blurple, custom_id="role_button_1")
    async def role_button_1(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._toggle(interaction, 1)

    @discord.ui.button(label="Role 3", style=discord.ButtonStyle.blurple, custom_id="role_button_2")
    async def role_button_2(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._toggle(interaction, 2)

    async def _toggle(self, interaction: discord.Interaction, index: int) -> None:
        role = interaction.guild.get_role(config["button_roles"][index])
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
    role_message = await channel.send(
        embed=discord.Embed(
            title="Self roles",
            description="Select or remove the role(s) that you want to be pinged in LFG posts.",
            color=discord.Color.blurple(),
        ),
        view=RolePanelView(guild, button_roles),
    )
    lfg_message = await channel.send(
        embed=discord.Embed(
            title="Looking for Group",
            description=(
                "Click **Start LFG** below to open a form.\n"
                "Your post will ping a role, open a thread and the thread expires after 1 hour."
            ),
            color=discord.Color.green(),
        ),
        view=LfgPanelView(),
    )
    return role_message, lfg_message


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
                f"Resets <t:{resets}:F> (<t:{resets}:R>)"
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
                f"Expires <t:{expires}:F> (<t:{expires}:R>)"
            ),
            inline=True,
        )
    else:
        embed.add_field(name="SP Void Cascade", value="❌ False", inline=True)
    embed.set_footer(text="browse.wf")
    return embed


class LfgSetupView(discord.ui.View):
    """Ephemeral LFG form: role to ping, player count, duration, optional note."""

    def __init__(self, roles: list[discord.Role]):
        super().__init__(timeout=600)
        self.role: discord.Role | None = None
        self.count: int | None = None
        self.duration: str | None = None
        self.note: str | None = None

        self.role_select = discord.ui.Select(
            placeholder="Which role should be pinged?",
            options=[discord.SelectOption(label=role.name, value=str(role.id)) for role in roles[:25]],
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

    def summary(self) -> str:
        return _summary(self.role, self.count, self.duration, self.note)

    def _count_options(self) -> list[discord.SelectOption]:
        max_players = 5 if self.role is not None and self.role.id == FIVE_PLAYER_ROLE_ID else 3
        return [
            discord.SelectOption(label=f"{n} player{'s' if n != 1 else ''}", value=str(n))
            for n in range(1, max_players + 1)
        ]

    async def on_role(self, interaction: discord.Interaction) -> None:
        self.role = interaction.guild.get_role(int(self.role_select.values[0]))
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

    async def _refresh(self, interaction: discord.Interaction) -> None:
        # Ephemeral messages can only be edited through the interaction response —
        # plain Message.edit would 404. The view rides along so rebuilt select
        # options (per-role player limit) show up.
        await interaction.response.edit_message(content=PANEL_INTRO + self.summary(), view=self)

    @discord.ui.button(label="Add a note", style=discord.ButtonStyle.grey, emoji="📝", row=3)
    async def add_note(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(NoteModal(view=self))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, row=3)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.delete_message()

    @discord.ui.button(label="Post LFG", style=discord.ButtonStyle.green, row=3)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.role is None or self.count is None or self.duration is None:
            await interaction.response.send_message(
                "Pick a role, a player count and a duration first.", ephemeral=True
            )
            return
        channel = interaction.guild.get_channel(config["channel_id"])
        if channel is None:
            await interaction.response.send_message(
                "The LFG channel isn't configured. Ask an admin to run `/setup`.", ephemeral=True
            )
            return
        created = dt.datetime.now(dt.timezone.utc)
        expires = created + dt.timedelta(hours=1)
        embed = discord.Embed(title=f"LFG — {self.role.name}", color=discord.Color.green())
        embed.add_field(name="Role", value=self.role.mention, inline=True)
        embed.add_field(name="Players needed", value=str(self.count), inline=True)
        embed.add_field(name="Duration", value=self.duration, inline=True)
        # A mention renders as a clickable @name without pinging.
        embed.add_field(name="Host", value=interaction.user.mention, inline=True)
        if self.note:
            embed.add_field(name="Note", value=self.note, inline=False)
        # Discord timestamps render in each viewer's local timezone.
        embed.add_field(
            name="Thread expires",
            value=f"<t:{int(expires.timestamp())}:F> (<t:{int(expires.timestamp())}:R>)",
            inline=False,
        )
        try:
            message = await channel.send(
                content=self.role.mention,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(roles=[self.role]),
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to post in the LFG channel.", ephemeral=True
            )
            return
        try:
            thread = await message.create_thread(
                name=f"LFG — {self.role.name} ({self.count} needed)"[:100],
                auto_archive_duration=60,
            )
        except discord.HTTPException as e:
            # Surface the real reason (e.g. thread rate limits) instead of a silent failure.
            await interaction.response.send_message(
                f"Posted the LFG, but opening a thread failed: {e}", ephemeral=True
            )
            return
        config["threads"][str(thread.id)] = {
            "created": created.isoformat(),
            "host": interaction.user.id,  # only they may use $close
        }
        save()
        self.clear_items()
        self.stop()
        await interaction.response.edit_message(content=f"✅ Posted! {thread.jump_url}", view=self)


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


class SetupState:
    def __init__(self):
        self.channel: discord.TextChannel | None = None
        self.ping_roles: list[discord.Role] = []
        self.admin_role: discord.Role | None = None


class SetupChannelView(discord.ui.View):
    """Setup page 1: LFG channel, pingable roles and the admin role."""

    def __init__(self, state: SetupState, guild: discord.Guild):
        super().__init__(timeout=300)
        self.state = state
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
        roles_default = [guild.get_role(rid) for rid in config["ping_roles"] if guild.get_role(rid)]
        self.roles_select = discord.ui.RoleSelect(
            placeholder="Roles that can be pinged in LFG posts",
            min_values=1,
            max_values=25,
            row=1,
            default_values=roles_default,
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
        # These selects are only read when "Next" is pressed — just acknowledge the pick.
        await interaction.response.defer()

    @discord.ui.button(label="Next →", style=discord.ButtonStyle.blurple, row=3)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Untouched selects return empty values — fall back to the shown defaults.
        channels = self.channel_select.values or self.channel_select.default_values
        roles = self.roles_select.values or self.roles_select.default_values
        if not channels or not roles:
            await interaction.response.send_message(
                "Pick the LFG channel and at least one pingable role first.", ephemeral=True
            )
            return
        # ChannelSelect.values gives a partial channel — resolve it to the real object.
        self.state.channel = interaction.guild.get_channel(channels[0].id)
        self.state.ping_roles = roles
        admin = self.admin_select.values or self.admin_select.default_values
        self.state.admin_role = admin[0] if admin else None
        await interaction.response.edit_message(
            content="**LFG setup** — page 2: pick the roles for the three self-role buttons.",
            view=SetupRolesView(self.state, interaction.guild),
        )


class SetupRolesView(discord.ui.View):
    """Setup page 2: the three role-button roles, then save & post the panels."""

    def __init__(self, state: SetupState, guild: discord.Guild):
        super().__init__(timeout=300)
        self.state = state
        self.role_selects: list[discord.ui.RoleSelect] = []
        for i in range(3):
            role_id = config["button_roles"][i]
            default = [guild.get_role(role_id)] if role_id and guild.get_role(role_id) else []
            select = discord.ui.RoleSelect(
                placeholder=f"Role for button {i + 1}",
                min_values=0,
                max_values=1,
                row=i,
                default_values=default,
            )
            select.callback = self._ack
            self.role_selects.append(select)
            self.add_item(select)

    async def _ack(self, interaction: discord.Interaction) -> None:
        # These selects are only read when "Save & post panels" is pressed.
        await interaction.response.defer()

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.grey, row=3)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="**LFG setup** — page 1: LFG channel, pingable roles and the admin role.",
            view=SetupChannelView(self.state, interaction.guild),
        )

    @discord.ui.button(label="Save & post panels", style=discord.ButtonStyle.green, row=3)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.state.channel is None:
            await interaction.response.send_message(
                "Go back and pick the LFG channel first.", ephemeral=True
            )
            return
        guild = interaction.guild
        # Untouched selects return empty values — fall back to the shown defaults.
        button_roles = []
        for select in self.role_selects:
            if select.values:
                button_roles.append(guild.get_role(select.values[0].id))
            elif select.default_values:
                button_roles.append(guild.get_role(select.default_values[0].id))
            else:
                button_roles.append(None)
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
        config["guild_id"] = guild.id
        config["channel_id"] = self.state.channel.id
        config["ping_roles"] = [role.id for role in self.state.ping_roles]
        config["admin_role_id"] = self.state.admin_role.id if self.state.admin_role else None
        config["button_roles"] = [role.id if role else None for role in button_roles]
        config["panel_message_id"] = role_message.id
        config["lfg_message_id"] = lfg_message.id
        save()
        await interaction.response.edit_message(
            content=f"✅ Saved! Panels posted in {self.state.channel.mention}.", view=None
        )
