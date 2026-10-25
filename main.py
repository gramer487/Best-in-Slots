"""LFG bot: self-role panel + LFG posts with threads (1h expiry) in a locked channel."""

import asyncio
import datetime as dt
import os
import traceback
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import modmail
import suggestions
from config import config, save
from modmail import ModmailCloseView, ModmailPanelView
from suggestions import weapon_autocomplete
from views import SetupMenuView, build_status_embed, post_panels
from worldstate import fetch_worldstate

intents = discord.Intents.default()
intents.message_content = True  # needed to read "$close" inside threads
intents.members = True  # keep the member cache populated for modmail lookups
bot = commands.Bot(command_prefix=None, intents=intents)
_status_task: asyncio.Task | None = None


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
    # Persistent views must be registered after every restart or their buttons die.
    bot.add_view(ModmailPanelView())
    bot.add_view(ModmailCloseView())
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching, name="do /suggestion for BiS req"
        )
    )
    guild = bot.get_guild(config["guild_id"]) if config["guild_id"] else None
    channel = guild.get_channel(config["channel_id"]) if guild and config["channel_id"] else None
    if channel is None:
        print("LFG channel not configured — fill in config.json or run /setup.")
    else:
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
    if not check_modmail_closes.is_running():
        check_modmail_closes.start()
    global _status_task
    if _status_task is None or _status_task.done():
        _status_task = bot.loop.create_task(status_task())
    print(f"Logged in as {bot.user} (bot user id: {bot.user.id})")
    print(f"In {len(bot.guilds)} guild(s): {[f'{g.name} ({g.id})' for g in bot.guilds]}")


@tasks.loop(minutes=1)
async def expire_threads():
    """Delete LFG threads 1 hour after creation, or at their scheduled $close time."""
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
        due = (delete_at is not None and now >= delete_at) or now - created >= dt.timedelta(hours=1)
        if not due:
            continue
        thread = channel.get_thread(int(thread_id))
        if thread is None:
            try:
                # guild.fetch_channel also resolves threads; channel.fetch_thread is not
                # available on older discord.py versions.
                thread = await guild.fetch_channel(int(thread_id))
            except discord.NotFound:
                thread = None
        if thread is not None:
            try:
                await thread.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                continue  # transient failure — keep the entry and retry next tick
        del config["threads"][thread_id]
        changed = True
    if changed:
        save()


@tasks.loop(minutes=1)
async def check_modmail_closes():
    """Close scheduled modmails when due (and warn their users 10 minutes before)."""
    await modmail.process_scheduled_closes(bot)


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
    return data


async def status_task():
    """Refresh the status embed, then sleep until the next arbitration/cascade change."""
    while not bot.is_closed():
        guild = bot.get_guild(config["guild_id"])
        channel = guild.get_channel(config["channel_id"]) if guild else None
        if channel is None:
            await asyncio.sleep(60)  # not configured yet
            continue
        data = await refresh_status(channel)
        if data is None:
            await asyncio.sleep(60)  # fetch failed — retry soon
            continue
        # Wake just after the earliest expiry: the arbitration rolls over hourly and
        # the cascade flag flips when its mission expires. The 10-minute cap catches
        # cascades that appear while none is live.
        now = dt.datetime.now(dt.timezone.utc)
        wake_at = now + dt.timedelta(minutes=10)
        if data.get("arbitration"):
            wake_at = min(wake_at, data["arbitration"]["rotates_at"])
        if data.get("sp_cascade"):
            wake_at = min(wake_at, data["sp_cascade"]["expiry"])
        await asyncio.sleep(max(30, (wake_at - now).total_seconds() + 10))


@bot.event
async def on_message(message: discord.Message):
    """DM modmail, channel lockdown and the $close command inside LFG threads."""
    if message.author.bot:
        return
    if message.guild is None:
        await modmail.handle_dm(message, bot.get_guild(config["guild_id"]))
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
        return
    if str(message.channel.id) in {str(cid) for cid in config["modmails"].values()}:
        await modmail.handle_staff(message)


@bot.tree.command(name="setup", description="Open the bot setup panel")
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
        "**Setup** — what do you want to configure?",
        view=SetupMenuView(interaction.guild),
        ephemeral=True,
    )


@bot.tree.command(name="suggestion", description="Suggest a weapon")
@app_commands.describe(weapon="Weapon to suggest", note="Optional note")
@app_commands.autocomplete(weapon=weapon_autocomplete)
async def suggestion(
    interaction: discord.Interaction, weapon: str, note: str | None = None
) -> None:
    resolved = suggestions.resolve_weapon(weapon)
    if resolved is None:
        await interaction.response.send_message(
            "That weapon isn't on the list — pick one from the autocomplete.", ephemeral=True
        )
        return
    key, entry = resolved
    display = entry["display"]
    if key in config["suggestions"]:
        await interaction.response.send_message(
            f"**{display}** has already been suggested.", ephemeral=True
        )
        return
    channel = (
        interaction.guild.get_channel(config["suggestion_channel_id"])
        if config["suggestion_channel_id"]
        else None
    )
    role = (
        interaction.guild.get_role(config["suggestion_ping_role_id"])
        if config["suggestion_ping_role_id"]
        else None
    )
    if channel is None or role is None:
        await interaction.response.send_message(
            "Suggestions aren't configured yet. Ask an admin to run `/setup`.", ephemeral=True
        )
        return
    embed = discord.Embed(title=display, color=discord.Color.blurple())
    if note and note.strip():
        embed.description = note.strip()[:4096]
    embed.add_field(name="Suggested by", value=f"<@!{interaction.user.id}>", inline=False)
    # Posting can take a moment — acknowledge while it happens.
    await interaction.response.defer(ephemeral=True)
    # Reserve the weapon before the network call so two people can't suggest it at once.
    config["suggestions"].append(key)
    save()
    try:
        await channel.send(
            content=role.mention,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(roles=[role]),
        )
    except discord.HTTPException:
        config["suggestions"].remove(key)
        save()
        await interaction.followup.send(
            "I couldn't post the suggestion — check my permissions in that channel.",
            ephemeral=True,
        )
        return
    await interaction.followup.send(f"✅ Successfully suggested **{display}**.", ephemeral=True)


@bot.tree.command(name="openthread", description="Open a modmail with a user")
@app_commands.describe(userid="ID of the user to contact")
async def openthread(interaction: discord.Interaction, userid: str) -> None:
    moderator = interaction.user if isinstance(interaction.user, discord.Member) else None
    if interaction.guild is None or moderator is None or not modmail.is_moderator(moderator):
        await interaction.response.send_message(
            "Only moderators can open a modmail.", ephemeral=True
        )
        return
    try:
        target_id = int(userid.strip())
    except ValueError:
        await interaction.response.send_message("That isn't a valid user ID.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    target = guild.get_member(target_id)
    if target is None:
        try:
            target = await guild.fetch_member(target_id)
        except discord.NotFound:
            target = None
        except discord.HTTPException:
            await interaction.followup.send(
                "Couldn't look that user up — try again.", ephemeral=True
            )
            return
    if target is None:
        await interaction.followup.send("That user isn't in the server.", ephemeral=True)
        return
    channel, created = await modmail.ensure_modmail_channel(guild, target)
    if channel is None:
        await interaction.followup.send(
            "Modmail isn't configured yet. Ask an admin to run `/setup`.", ephemeral=True
        )
        return
    if created:
        await modmail.announce_new_channel(channel, target)
    try:
        await target.send(
            "A moderator would like to speak with you. "
            "Reply to this DM and your message will reach the staff team."
        )
        dm_note = ""
    except discord.HTTPException:
        dm_note = "\nCouldn't DM them — they may have DMs closed."
    if created:
        result = f"Opened a modmail with {target.name} ({target.id}): {channel.mention}"
    else:
        result = f"A modmail with {target.name} ({target.id}) is already open: {channel.mention}"
    await interaction.followup.send(result + dm_note, ephemeral=True)


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
