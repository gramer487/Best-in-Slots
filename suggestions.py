"""Weapon suggestions: /suggestion autocomplete data and its /setup page."""

from __future__ import annotations

import json
from pathlib import Path

import discord

from config import config, save
from permissions import AdminOnlyView

WEAPONS_PATH = Path(__file__).with_name("weapons.json")


def _load_weapons() -> dict[str, dict]:
    try:
        with open(WEAPONS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        print(f"Couldn't load weapons.json: {e}")
        return {}


WEAPONS = _load_weapons()


async def weapon_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[discord.app_commands.Choice[str]]:
    """Matches on display name and internal id; startswith hits come first."""
    current = current.casefold()
    matches = [
        discord.app_commands.Choice(name=entry["display"], value=key)
        for key, entry in WEAPONS.items()
        if current in entry["display"].casefold() or current in key.casefold()
    ]
    matches.sort(key=lambda choice: not choice.name.casefold().startswith(current))
    return matches[:25]


def resolve_weapon(value: str) -> tuple[str, dict] | None:
    """Find a weapon by internal id or display name, case-insensitively."""
    if value in WEAPONS:
        return value, WEAPONS[value]
    folded = value.casefold()
    for key, entry in WEAPONS.items():
        if key.casefold() == folded or entry["display"].casefold() == folded:
            return key, entry
    return None


class SuggestionSetupView(AdminOnlyView):
    """Setup page: suggestion channel and the role pinged for new suggestions."""

    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=300)
        channel_default = (
            [guild.get_channel(config["suggestion_channel_id"])]
            if config["suggestion_channel_id"]
            and guild.get_channel(config["suggestion_channel_id"])
            else []
        )
        self.channel_select = discord.ui.ChannelSelect(
            placeholder="Channel for suggestion posts",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            row=0,
            default_values=channel_default,
        )
        role_default = (
            [guild.get_role(config["suggestion_ping_role_id"])]
            if config["suggestion_ping_role_id"]
            and guild.get_role(config["suggestion_ping_role_id"])
            else []
        )
        self.role_select = discord.ui.RoleSelect(
            placeholder="Role pinged for new suggestions",
            min_values=1,
            max_values=1,
            row=1,
            default_values=role_default,
        )
        for select in (self.channel_select, self.role_select):
            select.callback = self._ack
            self.add_item(select)

    async def _ack(self, interaction: discord.Interaction) -> None:
        # Selects are only read when "Save" is pressed — just acknowledge the pick.
        await interaction.response.defer()

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.grey, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from views import SetupMenuView

        await interaction.response.edit_message(
            content="**Setup** — what do you want to configure?",
            view=SetupMenuView(interaction.guild),
        )

    @discord.ui.button(label="Save suggestion setup", style=discord.ButtonStyle.green, row=2)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Untouched selects return empty values — fall back to the shown defaults.
        channels = self.channel_select.values or self.channel_select.default_values
        roles = self.role_select.values or self.role_select.default_values
        if not channels or not roles:
            await interaction.response.send_message(
                "Pick a channel and a role for suggestions first.", ephemeral=True
            )
            return
        config["suggestion_channel_id"] = channels[0].id
        config["suggestion_ping_role_id"] = roles[0].id
        save()
        channel = interaction.guild.get_channel(channels[0].id)
        role = interaction.guild.get_role(roles[0].id)
        await interaction.response.edit_message(
            content=f"✅ Suggestions saved! Posts go to #{channel.name} and ping {role.name}.",
            view=None,
        )
