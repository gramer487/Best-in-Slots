"""LFG bot: self-role panel + LFG posts with threads (1h expiry) in a locked channel."""

import datetime as dt
import os
from pathlib import Path

import discord
from discord.ext import commands, tasks

from config import config, save
from views import LfgPanelView, RolePanelView, SetupChannelView, SetupState

intents = discord.Intents.default()
bot = commands.Bot(command_prefix=None, intents=intents)


def get_token() -> str:
    token = os.environ.get("DISCORD_TOKEN")
    if token:
        return token
    env_path = Path(__file__).with_name(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("DISCORD_TOKEN="):
                return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("Put DISCORD_TOKEN=your-token in the .env file next to main.py.")


@bot.event
async def on_ready():
    guild = bot.get_guild(config["guild_id"]) if config["guild_id"] else None
    if guild:
        if config["panel_message_id"]:
            bot.add_view(RolePanelView(guild), message_id=config["panel_message_id"])
        if config["lfg_message_id"]:
            bot.add_view(LfgPanelView(), message_id=config["lfg_message_id"])
    if bot.guilds:
        for g in bot.guilds:
            await bot.tree.sync(guild=g)
    else:
        await bot.tree.sync()
    if not expire_threads.is_running():
        expire_threads.start()
    print(f"Logged in as {bot.user}")


@tasks.loop(minutes=5)
async def expire_threads():
    """Archive and lock LFG threads 1 hour after creation."""
    guild = bot.get_guild(config["guild_id"])
    if guild is None:
        return
    channel = guild.get_channel(config["channel_id"])
    if channel is None:
        return
    now = dt.datetime.now(dt.timezone.utc)
    changed = False
    for thread_id in list(config["threads"]):
        created = dt.datetime.fromisoformat(config["threads"][thread_id])
        thread = channel.get_thread(int(thread_id))
        if thread is not None and not thread.archived and now - created >= dt.timedelta(hours=1):
            try:
                await thread.edit(archived=True, locked=True)
            except discord.HTTPException:
                pass
        if (thread is not None and thread.archived) or now - created >= dt.timedelta(hours=26):
            del config["threads"][thread_id]
            changed = True
    if changed:
        save()


@bot.event
async def on_message(message: discord.Message):
    """The LFG channel only hosts the bot's panels and posts — delete everything else."""
    if message.author.bot or message.guild is None:
        return
    if message.channel.id == config["channel_id"] and isinstance(message.channel, discord.TextChannel):
        try:
            await message.delete()
        except discord.HTTPException:
            pass


@bot.tree.command(name="setup", description="Open the LFG bot setup panel")
async def setup(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "You need the Administrator permission to use this.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        "**LFG setup** — page 1: pick the LFG channel and the roles LFG posts may ping.",
        view=SetupChannelView(SetupState()),
        ephemeral=True,
    )


if __name__ == "__main__":
    bot.run(get_token())
