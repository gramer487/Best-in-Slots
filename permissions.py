"""Shared permission checks for setup pages."""

from __future__ import annotations

import discord

from config import config


def is_admin(member: discord.Member) -> bool:
    """Administrator permission or the configured /setup admin role."""
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    role_id = config["admin_role_id"]
    return bool(role_id) and any(role.id == role_id for role in member.roles)


class AdminOnlyView(discord.ui.View):
    """Setup pages stay admin-only even when the panel itself is public."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if isinstance(interaction.user, discord.Member) and is_admin(interaction.user):
            return True
        await interaction.response.send_message(
            "Only administrators can use setup.", ephemeral=True
        )
        return False
