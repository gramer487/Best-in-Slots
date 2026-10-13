"""LFG bot: self-role panel + LFG posts with threads (1h expiry) in a locked channel."""

import datetime as dt
import os
import traceback
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands, tasks

from config import config, save
from views import SetupChannelView, SetupState, build_status_embed, post_panels
from worldstate import fetch_worldstate

intents = discord.Intents.default()
intents.message_content = True  # needed to read "$close" inside threads
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
        channel = guild.get_channel(config["channel_id"]) if config["channel_id"] else None
        if channel is not None:
            # Fresh start: clear the whole channel, then repost the panels.
            try:
                await channel.purge(limit=None)
            except discord.HTTPException:
                pass
            try:
                role_message, lfg_message = await post_panels(channel, guild)
            except discord.Forbidden:
                print("Can't post panels in the LFG channel — check the bot's permissions there.")
            else:
                config["panel_message_id"] = role_message.id
                config["lfg_message_id"] = lfg_message.id
                save()
            await refresh_status(channel)
    try:
        if bot.guilds:
            for g in bot.guilds:
                bot.tree.copy_global_to(guild=g)
                synced = await bot.tree.sync(guild=g)
                print(f"sync returned: {[c.name for c in synced]}")
                registered = await bot.tree.fetch_commands(guild=g)
                print(f"fetch returned: {[c.name for c in registered]}")
        else:
            await bot.tree.sync()
    except discord.HTTPException as e:
        print(f"Command sync failed: {e}")
        print("The bot was probably invited without the 'applications.commands' scope — "
              "re-invite it with the URL from the README and restart.")
    if not expire_threads.is_running():
        expire_threads.start()
    if not status_loop.is_running():
        status_loop.start()
    print(f"Logged in as {bot.user} (bot user id: {bot.user.id})")
    print(f"In {len(bot.guilds)} guild(s): {[f'{g.name} ({g.id})' for g in bot.guilds]}")


@tasks.loop(minutes=1)
async def expire_threads():
    """Archive + lock LFG threads 1 hour after creation; delete $close-requested threads."""
    guild = bot.get_guild(config["guild_id"])
    if guild is None:
        return
    channel = guild.get_channel(config["channel_id"])
    if channel is None:
        return
    now = dt.datetime.now(dt.timezone.utc)
    changed = False
    for thread_id in list(config["threads"]):
        entry = config["threads"][thread_id]
        if isinstance(entry, str):  # entries from older configs only had a timestamp
            entry = {"created": entry}
        created = dt.datetime.fromisoformat(entry["created"])
        delete_at = (
            dt.datetime.fromisoformat(entry["delete_at"]) if entry.get("delete_at") else None
        )
        thread = channel.get_thread(int(thread_id))
        if delete_at is not None and now >= delete_at:
            if thread is None:
                try:
                    thread = await channel.fetch_thread(int(thread_id))
                except discord.NotFound:
                    thread = None
            if thread is not None:
                try:
                    await thread.delete()
                except discord.HTTPException:
                    pass
            del config["threads"][thread_id]
            changed = True
            continue
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


async def refresh_status(channel) -> None:
    """Fetch the world state and post/update the status embed in the LFG channel."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            data = await fetch_worldstate(session)
    except (aiohttp.ClientError, ValueError) as e:
        print(f"World state fetch failed: {e}")
        return
    message = None
    if config["status_message_id"]:
        try:
            message = await channel.fetch_message(config["status_message_id"])
        except discord.NotFound:
            message = None  # deleted (e.g. by the restart purge) — repost
    try:
        if message is None:
            message = await channel.send(embed=build_status_embed(data))
            config["status_message_id"] = message.id
            save()
        else:
            await message.edit(embed=build_status_embed(data))
    except discord.HTTPException as e:
        print(f"Status embed update failed: {e}")


@tasks.loop(minutes=5)
async def status_loop():
    guild = bot.get_guild(config["guild_id"])
    if guild is None:
        return
    channel = guild.get_channel(config["channel_id"])
    if channel is None:
        return
    await refresh_status(channel)


@bot.event
async def on_message(message: discord.Message):
    """Channel lockdown and the $close command inside LFG threads."""
    if message.author.bot or message.guild is None:
        return
    if message.channel.id == config["channel_id"] and isinstance(message.channel, discord.TextChannel):
        # The LFG channel only hosts the bot's panels and posts — delete everything else.
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        return
    if (
        isinstance(message.channel, discord.Thread)
        and message.content.strip() == "$close"
        and str(message.channel.id) in config["threads"]
    ):
        thread_id = str(message.channel.id)
        entry = config["threads"][thread_id]
        if isinstance(entry, str):  # entries from older configs only had a timestamp
            entry = {"created": entry}
        if entry.get("delete_at"):
            return  # already scheduled for deletion
        if entry.get("host") is not None and message.author.id != entry["host"]:
            return  # only the thread owner can close it
        entry["delete_at"] = (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)
        ).isoformat()
        config["threads"][thread_id] = entry
        save()
        await message.channel.send("🗑️ This thread will be deleted in 10 minutes.")


@bot.tree.command(name="setup", description="Open the LFG bot setup panel")
async def setup(interaction: discord.Interaction):
    admin_role = (
        interaction.guild.get_role(config["admin_role_id"]) if config["admin_role_id"] else None
    )
    is_admin = interaction.user.guild_permissions.administrator
    has_admin_role = admin_role is not None and admin_role in interaction.user.roles
    if not is_admin and not has_admin_role:
        await interaction.response.send_message(
            "You need the Administrator permission or the configured admin role to use this.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        "**LFG setup** — page 1: LFG channel, pingable roles and the admin role.",
        view=SetupChannelView(SetupState(), interaction.guild),
        ephemeral=True,
    )


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    traceback.print_exception(type(error), error, error.__traceback__)
    message = f"Something went wrong: {error}"
    if not interaction.response.is_done():
        await interaction.response.send_message(message, ephemeral=True)
    else:
        await interaction.followup.send(message, ephemeral=True)


if __name__ == "__main__":
    bot.run(get_token())
