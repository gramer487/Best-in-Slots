"""Applications: setup pages, Apply panels, the DM question flow and reviewer decisions."""

from __future__ import annotations

import copy
import datetime as dt
import uuid

import discord

import modmail
from config import config, save
from permissions import AdminOnlyView

APPLY_PREFIX = "apply:"
REVIEW_APPROVE_ID = "application_approve"
REVIEW_DENY_ID = "application_deny"
THREAD_ARCHIVE = 10080  # 7 days, Discord's maximum
SESSION_TIMEOUT = dt.timedelta(minutes=30)
MAX_CHOICES = 25

# user_id -> live DM session (not persisted; a restart drops half-filled applications)
_sessions: dict[int, dict] = {}


# --------------------------------------------------------------------------- data helpers


def build_panel_embed(guild: discord.Guild, app: dict) -> discord.Embed:
    role = guild.get_role(app["min_role_id"]) if app.get("min_role_id") else None
    embed = discord.Embed(
        title=app["title"][:256],
        description=app["description"][:4096],
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Required role", value=role.name if role else "Unknown role", inline=True)
    embed.add_field(name="Questions", value=str(len(app["questions"])), inline=True)
    embed.set_footer(text="Press Apply to start")
    return embed


def _pack_embeds(
    title: str, description: str, fields: list[tuple[str, str]], color: discord.Color
) -> list[discord.Embed]:
    """Split fields across embeds so nothing exceeds Discord's per-embed limits."""
    title = (title or "Application")[:256]
    description = (description or "")[:4096]
    embeds = [discord.Embed(title=title, description=description or None, color=color)]
    used = len(description)
    for name, value in fields:
        name = (" ".join((name or "Question").split()) or "Question")[:256]
        value = (value or "—")[:1024]
        cost = len(name) + len(value)
        current = embeds[-1]
        if len(current.fields) >= 25 or (current.fields and used + cost > 5500):
            current = discord.Embed(title=f"{title} (cont.)"[:256], color=color)
            embeds.append(current)
            used = 0
        current.add_field(name=name, value=value, inline=False)
        used += cost
    if len(embeds) > 10:
        embeds = embeds[:10]
        last = embeds[-1]
        if len(last.fields) >= 25:
            last.remove_field(24)
        last.add_field(name="Truncated", value="Too many answers to show in full.", inline=False)
    return embeds


def build_submission_embeds(sub: dict) -> list[discord.Embed]:
    status = sub.get("status", "pending")
    if status == "approved":
        status_text = f"Approved by {sub.get('reviewer_name', 'a moderator')}"
        color = discord.Color.green()
    elif status == "denied":
        status_text = f"Denied by {sub.get('reviewer_name', 'a moderator')}"
        color = discord.Color.red()
    else:
        status_text = "Pending review"
        color = discord.Color.blurple()
    fields = [
        ("Applicant", f"{sub.get('user_name', 'Unknown')} ({sub['user_id']})"),
        ("Status", status_text),
    ]
    for index, (question, answer) in enumerate(sub["answers"], 1):
        fields.append((f"{index}. {question}", answer))
    return _pack_embeds(sub.get("title", "Application"), sub.get("description", ""), fields, color)


def application_details(guild: discord.Guild, app_id: str, app: dict) -> str:
    role = guild.get_role(app["min_role_id"]) if app.get("min_role_id") else None
    lines = [
        f"**{app['title']}** (`{app_id}`)",
        f"**Required role:** {role.name if role else 'missing'}",
        f"**Description:** {app['description']}",
        "**Questions:**",
    ]
    for index, question in enumerate(app["questions"], 1):
        if question["options"]:
            lines.append(f"{index}. [choice] {question['text']} — {', '.join(question['options'])}")
        else:
            lines.append(f"{index}. [text] {question['text']}")
    return "\n".join(lines)[:2000]


def parse_options(raw: str) -> tuple[list[str] | None, str | None]:
    """Turn the modal's one-per-line choices into select options (or an error message)."""
    options = [line.strip() for line in raw.splitlines() if line.strip()]
    if not options:
        return None, None
    if len(options) < 2:
        return None, "A multiple-choice question needs at least two choices (one per line)."
    if len(options) > MAX_CHOICES:
        return None, f"A question can have at most {MAX_CHOICES} choices."
    if any(len(option) > 100 for option in options):
        return None, "Each choice must be 100 characters or fewer."
    if len(set(options)) != len(options):
        return None, "Choices must be unique."
    return options, None


def _thread_name(title: str, user: discord.abc.User) -> str:
    name = f"{title} - {user.name}".strip()
    return name[:100] or "Application"


def _chunks(text: str, limit: int):
    while text:
        if len(text) <= limit:
            yield text
            return
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        yield text[:cut]
        text = text[cut:].lstrip("\n")


# --------------------------------------------------------------------------- DM question flow


async def _send_question(user: discord.abc.User, session: dict) -> None:
    question = session["questions"][session["index"]]
    total = len(session["questions"])
    header = (
        f"**{session['title']}** — question {session['index'] + 1} of {total}\n{question['text']}"
    )
    if question["options"]:
        session["state"] = "choice"
        await user.send(header, view=ChoiceView(user, session, question["options"]))
    else:
        session["state"] = "text"
        await user.send(header + "\n\nReply with your answer. Type `cancel` to stop.")


async def _send_confirmation(user: discord.abc.User, session: dict) -> None:
    lines = [f"**{session['title']}**", "", "Review your answers:"]
    for index, (question, answer) in enumerate(
        zip(session["questions"], session["answers"]), 1
    ):
        lines.append(f"\n**{index}. {question['text']}**\n{answer}")
    for chunk in _chunks("\n".join(lines), 1900):
        await user.send(chunk)
    await user.send(
        "Send this application to the moderators?", view=ApplicationConfirmView(user, session)
    )


async def _advance(user: discord.abc.User, session: dict) -> None:
    # Block a second DM that arrives while the next question is being sent.
    session["state"] = "processing"
    session["index"] += 1
    session["updated"] = dt.datetime.now(dt.timezone.utc)
    try:
        if session["index"] >= len(session["questions"]):
            session["state"] = "confirm"
            await _send_confirmation(user, session)
        else:
            await _send_question(user, session)
    except discord.HTTPException:
        # The user closed DMs mid-application — drop it rather than stay stuck.
        _sessions.pop(user.id, None)


async def handle_dm(message: discord.Message) -> bool:
    """Consume a DM that is part of an application. Returns True when it was handled."""
    session = _sessions.get(message.author.id)
    if session is None:
        return False
    content = message.content.strip()
    if content.lower() == "cancel":
        _sessions.pop(message.author.id, None)
        await message.channel.send("Application cancelled.")
        return True
    if session["state"] == "text":
        if not content:
            await message.channel.send("Please send a text answer, or type `cancel` to stop.")
            return True
        session["answers"].append(content[:2000])
        session["updated"] = dt.datetime.now(dt.timezone.utc)
        await _advance(message.author, session)
        return True
    if session["state"] == "choice":
        await message.channel.send(
            "Please pick one of the options above, or type `cancel` to stop."
        )
        return True
    if session["state"] == "confirm":
        await message.channel.send("Press Confirm or Cancel above, or type `cancel` to stop.")
        return True
    if session["state"] == "processing":
        # A message raced the one we're already handling — ignore it.
        return True
    return True


class ChoiceView(discord.ui.View):
    """Dropdown for one multiple-choice question in DMs."""

    def __init__(self, user: discord.abc.User, session: dict, options: list[str]):
        super().__init__(timeout=1800)
        self.user = user
        self.session = session
        self.index = session["index"]
        select = discord.ui.Select(
            placeholder="Choose an answer",
            options=[discord.SelectOption(label=o[:100], value=o[:100]) for o in options],
            min_values=1,
            max_values=1,
        )
        select.callback = self._chosen
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "This isn't your application.", ephemeral=True
            )
            return False
        return True

    async def _chosen(self, interaction: discord.Interaction) -> None:
        session = _sessions.get(self.user.id)
        if (
            session is None
            or session is not self.session
            or session.get("index") != self.index
            or session.get("state") != "choice"
        ):
            await interaction.response.send_message(
                "This question is no longer active.", ephemeral=True
            )
            return
        answer = interaction.data["values"][0]
        session["answers"].append(answer)
        session["updated"] = dt.datetime.now(dt.timezone.utc)
        await interaction.response.edit_message(
            content=f"{interaction.message.content or ''}\n\n**Your answer:** {answer}",
            view=None,
        )
        await _advance(self.user, session)

    async def on_timeout(self) -> None:
        session = _sessions.get(self.user.id)
        # Only the view for the question still being asked may time the session out.
        if (
            session is self.session
            and session.get("index") == self.index
            and session.get("state") == "choice"
        ):
            _sessions.pop(self.user.id, None)
            try:
                await self.user.send("This application timed out. Press Apply to start again.")
            except discord.HTTPException:
                pass


class ApplicationConfirmView(discord.ui.View):
    """Final Confirm / Cancel prompt in DMs."""

    def __init__(self, user: discord.abc.User, session: dict):
        super().__init__(timeout=1800)
        self.user = user
        self.session = session

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "This isn't your application.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        session = _sessions.get(self.user.id)
        if session is not self.session or session.get("state") != "confirm":
            await interaction.response.send_message(
                "This application is no longer active.", ephemeral=True
            )
            return
        app = config["applications"].get(session["app_id"])
        guild = (
            interaction.client.get_guild(config["guild_id"]) if config["guild_id"] else None
        )
        channel = (
            guild.get_channel(config["applications_review_channel_id"])
            if guild and config["applications_review_channel_id"]
            else None
        )
        if app is None or guild is None or channel is None:
            _sessions.pop(self.user.id, None)
            await interaction.response.edit_message(
                content="This application can't be submitted right now — contact the staff directly.",
                view=None,
            )
            return
        # Reserve the session so a double click or a Cancel can't race the submission.
        session["state"] = "processing"
        await interaction.response.defer()
        submission = {
            "app_id": session["app_id"],
            "title": session["title"],
            "description": session["description"],
            "user_id": self.user.id,
            "user_name": self.user.name,
            "answers": [
                [question["text"], answer]
                for question, answer in zip(session["questions"], session["answers"])
            ],
            "status": "pending",
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "review_channel_id": channel.id,
        }
        embeds = build_submission_embeds(submission)
        try:
            message = await channel.send(embeds=embeds, view=ApplicationReviewView())
        except discord.HTTPException:
            _sessions.pop(self.user.id, None)
            await interaction.message.edit(
                content="I couldn't submit your application — please contact the staff directly.",
                view=None,
            )
            return
        thread = None
        try:
            thread = await message.create_thread(
                name=_thread_name(session["title"], self.user),
                auto_archive_duration=THREAD_ARCHIVE,
            )
            thread_message = await thread.send(embeds=embeds, view=ApplicationReviewView())
        except discord.HTTPException:
            if thread is not None:
                try:
                    await thread.delete()
                except discord.HTTPException:
                    pass
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            _sessions.pop(self.user.id, None)
            await interaction.message.edit(
                content="I couldn't submit your application — please contact the staff directly.",
                view=None,
            )
            return
        submission["channel_message_id"] = message.id
        submission["thread_id"] = thread.id
        submission["thread_message_id"] = thread_message.id
        config["application_submissions"][str(thread.id)] = submission
        save()
        _sessions.pop(self.user.id, None)
        for sent in (message, thread_message):
            for emoji in ("✅", "❌"):
                try:
                    await sent.add_reaction(emoji)
                except discord.HTTPException:
                    pass
        await interaction.message.edit(
            content="Application submitted. The staff will review it.", view=None
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        session = _sessions.get(self.user.id)
        if session is not self.session or session.get("state") != "confirm":
            await interaction.response.send_message(
                "This application is no longer active.", ephemeral=True
            )
            return
        _sessions.pop(self.user.id, None)
        await interaction.response.edit_message(content="Application cancelled.", view=None)

    async def on_timeout(self) -> None:
        session = _sessions.get(self.user.id)
        if session is self.session:
            _sessions.pop(self.user.id, None)
            try:
                await self.user.send("This application timed out. Press Apply to start again.")
            except discord.HTTPException:
                pass


# --------------------------------------------------------------------------- apply panel


class ApplicationApplyView(discord.ui.View):
    """The Apply button under one application's public embed."""

    def __init__(self, app_id: str):
        super().__init__(timeout=None)
        self.app_id = app_id
        button = discord.ui.Button(
            label="Apply", style=discord.ButtonStyle.green, custom_id=f"{APPLY_PREFIX}{app_id}"
        )
        button.callback = self._apply
        self.add_item(button)

    async def _apply(self, interaction: discord.Interaction) -> None:
        app = config["applications"].get(self.app_id)
        if app is None:
            await interaction.response.send_message(
                "This application is no longer available.", ephemeral=True
            )
            return
        member = interaction.user
        if not isinstance(member, discord.Member) or interaction.guild is None:
            await interaction.response.send_message("Apply from inside the server.", ephemeral=True)
            return
        role_id = app.get("min_role_id")
        if role_id and not any(role.id == role_id for role in member.roles):
            role = interaction.guild.get_role(role_id)
            await interaction.response.send_message(
                f"You need the **{role.name if role else 'required'}** role to apply.",
                ephemeral=True,
            )
            return
        if not app.get("questions"):
            await interaction.response.send_message(
                "This application has no questions yet — ask an admin to finish setting it up.",
                ephemeral=True,
            )
            return
        existing = _sessions.get(member.id)
        now = dt.datetime.now(dt.timezone.utc)
        if existing is not None:
            if now - existing["updated"] < SESSION_TIMEOUT:
                await interaction.response.send_message(
                    "You're already filling out an application — check your DMs. "
                    "Type `cancel` there to stop.",
                    ephemeral=True,
                )
                return
            _sessions.pop(member.id, None)
        # Reserve before deferring so a double click can't start two sessions.
        session = {
            "app_id": self.app_id,
            "title": app["title"],
            "description": app["description"],
            "questions": copy.deepcopy(app["questions"]),
            "index": 0,
            "answers": [],
            "state": "text",
            "updated": now,
        }
        _sessions[member.id] = session
        await interaction.response.defer(ephemeral=True)
        try:
            await _send_question(member, session)
        except discord.HTTPException:
            _sessions.pop(member.id, None)
            await interaction.followup.send(
                "I couldn't DM you — open your DMs to server members and try again.",
                ephemeral=True,
            )
            return
        await interaction.followup.send("Check your DMs to answer the questions.", ephemeral=True)


# --------------------------------------------------------------------------- review


class ApplicationReviewView(discord.ui.View):
    """Approve / Deny buttons on submission embeds (persistent, custom_id routed)."""

    def __init__(self, disabled: bool = False):
        super().__init__(timeout=None)
        for label, style, emoji, custom_id, approved in (
            ("Approve", discord.ButtonStyle.success, "✅", REVIEW_APPROVE_ID, True),
            ("Deny", discord.ButtonStyle.danger, "❌", REVIEW_DENY_ID, False),
        ):
            button = discord.ui.Button(
                label=label, style=style, emoji=emoji, custom_id=custom_id, disabled=disabled
            )
            button.callback = self._make_callback(approved)
            self.add_item(button)

    def _make_callback(self, approved: bool):
        async def callback(interaction: discord.Interaction) -> None:
            await self._decide(interaction, approved)

        return callback

    async def _decide(self, interaction: discord.Interaction, approved: bool) -> None:
        if (
            not isinstance(interaction.user, discord.Member)
            or interaction.guild is None
            or not modmail.is_moderator(interaction.user)
        ):
            await interaction.response.send_message(
                "Only moderators can review applications.", ephemeral=True
            )
            return
        submission = _find_submission(interaction.message.id)
        if submission is None:
            await interaction.response.send_message(
                "This application is no longer active.", ephemeral=True
            )
            return
        if submission.get("status", "pending") != "pending":
            await interaction.response.send_message(
                "This application has already been decided.", ephemeral=True
            )
            return
        word = "approved" if approved else "denied"
        await interaction.response.send_message(f"Application {word}.", ephemeral=True)
        submission["status"] = "approved" if approved else "denied"
        submission["reviewer_id"] = interaction.user.id
        submission["reviewer_name"] = interaction.user.display_name
        save()
        guild = interaction.guild
        embeds = build_submission_embeds(submission)
        thread = guild.get_channel(int(submission["thread_id"]))
        if thread is None:
            try:
                thread = await guild.fetch_channel(int(submission["thread_id"]))
            except discord.HTTPException:
                thread = None
        channel = guild.get_channel(submission["review_channel_id"])
        if channel is None:
            try:
                channel = await guild.fetch_channel(submission["review_channel_id"])
            except discord.HTTPException:
                channel = None
        # The in-thread copy is the lasting record; the post in the review channel is
        # removed so decided applications don't pile up. Deleting the message a thread
        # was started from only orphans the thread — it stays archived with its messages.
        record_updated = False
        if thread is not None:
            try:
                message = await thread.fetch_message(submission["thread_message_id"])
                await message.edit(embeds=embeds, view=ApplicationReviewView(disabled=True))
                record_updated = True
            except discord.HTTPException:
                pass
        if channel is not None:
            try:
                message = await channel.fetch_message(submission["channel_message_id"])
                if record_updated:
                    await message.delete()
                else:
                    # No usable thread copy — keep the decision visible here instead.
                    await message.edit(embeds=embeds, view=ApplicationReviewView(disabled=True))
            except discord.HTTPException:
                pass
        dm_failed = False
        try:
            applicant = interaction.client.get_user(submission["user_id"])
            if applicant is None:
                applicant = await interaction.client.fetch_user(submission["user_id"])
            await applicant.send(
                f"Your application for **{submission['title']}** was {word}."
            )
        except discord.HTTPException:
            dm_failed = True
        if dm_failed and thread is not None:
            try:
                await thread.send("Couldn't DM the applicant about the decision.")
            except discord.HTTPException:
                pass
        if thread is not None:
            try:
                await thread.edit(archived=True, locked=True)
            except discord.HTTPException:
                await interaction.followup.send(
                    "The decision was saved but I couldn't close the thread — "
                    "check my Manage Threads permission.",
                    ephemeral=True,
                )


def _find_submission(message_id: int) -> dict | None:
    for submission in config["application_submissions"].values():
        if message_id in (
            submission.get("channel_message_id"),
            submission.get("thread_message_id"),
        ):
            return submission
    return None


def register_views(bot: discord.Client) -> None:
    """Re-attach persistent views after every restart."""
    bot.add_view(ApplicationReviewView())
    for app_id in config["applications"]:
        bot.add_view(ApplicationApplyView(app_id))


# --------------------------------------------------------------------------- /setup pages


class ApplicationsSetupView(AdminOnlyView):
    """Applications setup: channels, create, manage."""

    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=1800)
        self.guild = guild
        post_default = (
            [guild.get_channel(config["applications_post_channel_id"])]
            if config["applications_post_channel_id"]
            and guild.get_channel(config["applications_post_channel_id"])
            else []
        )
        review_default = (
            [guild.get_channel(config["applications_review_channel_id"])]
            if config["applications_review_channel_id"]
            and guild.get_channel(config["applications_review_channel_id"])
            else []
        )
        self.post_select = discord.ui.ChannelSelect(
            placeholder="Post channel (Apply panels)",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            row=0,
            default_values=post_default,
        )
        self.review_select = discord.ui.ChannelSelect(
            placeholder="Review channel (submissions + threads)",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            row=1,
            default_values=review_default,
        )
        for select in (self.post_select, self.review_select):
            select.callback = self._ack
            self.add_item(select)
        for label, style, callback in (
            ("Save channels", discord.ButtonStyle.green, self._save),
            ("Create application", discord.ButtonStyle.blurple, self._create),
            ("Manage applications", discord.ButtonStyle.grey, self._manage),
            ("← Back", discord.ButtonStyle.grey, self._back),
        ):
            button = discord.ui.Button(label=label, style=style, row=2)
            button.callback = callback
            self.add_item(button)

    async def _ack(self, interaction: discord.Interaction) -> None:
        # Selects are read on Save — just acknowledge the pick.
        await interaction.response.defer()

    async def _save(self, interaction: discord.Interaction) -> None:
        posts = self.post_select.values or self.post_select.default_values
        reviews = self.review_select.values or self.review_select.default_values
        if not posts or not reviews:
            await interaction.response.send_message(
                "Pick both a post channel and a review channel first.", ephemeral=True
            )
            return
        post = self.guild.get_channel(posts[0].id)
        review = self.guild.get_channel(reviews[0].id)
        if post is None or review is None:
            await interaction.response.send_message(
                "That channel no longer exists — pick another.", ephemeral=True
            )
            return
        await interaction.response.defer()
        old_post_id = config["applications_post_channel_id"]
        config["applications_post_channel_id"] = post.id
        config["applications_review_channel_id"] = review.id
        save()
        note = ""
        if old_post_id != post.id and config["applications"]:
            old_channel = self.guild.get_channel(old_post_id) if old_post_id else None
            for app_id, app in list(config["applications"].items()):
                old_message = None
                if old_channel is not None and app.get("panel_message_id"):
                    try:
                        old_message = await old_channel.fetch_message(app["panel_message_id"])
                    except discord.HTTPException:
                        old_message = None
                try:
                    await _post_panel(post, app_id, app)
                except discord.HTTPException:
                    note = "\nSome panels couldn't be posted — check my permissions."
                    break
                if old_message is not None:
                    try:
                        await old_message.delete()
                    except discord.HTTPException:
                        pass
            save()
        await interaction.edit_original_response(
            content=(
                f"Saved. Panels post in {post.mention}; submissions go to {review.mention}.{note}"
            ),
            view=ApplicationsSetupView(self.guild),
        )

    async def _create(self, interaction: discord.Interaction) -> None:
        post = (
            self.guild.get_channel(config["applications_post_channel_id"])
            if config["applications_post_channel_id"]
            else None
        )
        review = (
            self.guild.get_channel(config["applications_review_channel_id"])
            if config["applications_review_channel_id"]
            else None
        )
        if post is None or review is None:
            await interaction.response.send_message(
                "Save both channels first.", ephemeral=True
            )
            return
        builder = ApplicationBuilderView(self.guild)
        await interaction.response.edit_message(content=builder.summary(), view=builder)

    async def _manage(self, interaction: discord.Interaction) -> None:
        if not config["applications"]:
            await interaction.response.send_message(
                "There are no applications yet — create one first.", ephemeral=True
            )
            return
        await interaction.response.edit_message(
            content="**Manage applications** — pick one to edit or remove.",
            view=ManageApplicationsView(self.guild),
        )

    async def _back(self, interaction: discord.Interaction) -> None:
        from views import SetupMenuView

        await interaction.response.edit_message(
            content="**Setup** — what do you want to configure?",
            view=SetupMenuView(self.guild),
        )


class ApplicationBuilderView(AdminOnlyView):
    """Create/edit draft held in memory until Save."""

    def __init__(self, guild: discord.Guild, existing: tuple[str, dict] | None = None):
        super().__init__(timeout=1800)
        self.guild = guild
        self.app_id: str | None = None
        if existing is None:
            self.title = ""
            self.description = ""
            self.min_role_id: int | None = None
            self.questions: list[dict] = []
        else:
            self.app_id, app = existing
            self.title = app["title"]
            self.description = app["description"]
            self.min_role_id = app.get("min_role_id")
            self.questions = copy.deepcopy(app["questions"])
        role_default = (
            [guild.get_role(self.min_role_id)]
            if self.min_role_id and guild.get_role(self.min_role_id)
            else []
        )
        self.role_select = discord.ui.RoleSelect(
            placeholder="Role required to apply",
            min_values=1,
            max_values=1,
            row=0,
            default_values=role_default,
        )
        self.role_select.callback = self._role_chosen
        self.add_item(self.role_select)
        for label, style, row, callback in (
            ("Title & description", discord.ButtonStyle.blurple, 1, self._metadata),
            ("Add question", discord.ButtonStyle.green, 1, self._add_question),
            ("Remove question", discord.ButtonStyle.grey, 1, self._remove_question),
            ("Save application", discord.ButtonStyle.green, 2, self._save),
            ("Cancel", discord.ButtonStyle.grey, 2, self._cancel),
        ):
            button = discord.ui.Button(label=label, style=style, row=row)
            button.callback = callback
            self.add_item(button)
            if label == "Remove question":
                self.remove_button = button
        self.remove_button.disabled = not self.questions

    def summary(self) -> str:
        role = self.guild.get_role(self.min_role_id) if self.min_role_id else None
        lines = [
            f"**{'Edit' if self.app_id else 'New'} application**",
            f"**Title:** {self.title or '*not set*'}",
            f"**Required role:** {role.name if role else '*not set*'}",
            f"**Description:** {self.description or '*not set*'}",
            "**Questions:**",
        ]
        if not self.questions:
            lines.append("*none yet — add at least one*")
        for index, question in enumerate(self.questions, 1):
            if question["options"]:
                lines.append(
                    f"{index}. [choice] {question['text']} — {', '.join(question['options'])}"
                )
            else:
                lines.append(f"{index}. [text] {question['text']}")
        return "\n".join(lines)[:2000]

    async def _role_chosen(self, interaction: discord.Interaction) -> None:
        roles = self.role_select.values
        if roles:
            self.min_role_id = roles[0].id
        await interaction.response.edit_message(content=self.summary(), view=self)

    async def _metadata(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(MetadataModal(self))

    async def _add_question(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(QuestionModal(self))

    async def _remove_question(self, interaction: discord.Interaction) -> None:
        if not self.questions:
            await interaction.response.send_message("There are no questions to remove.", ephemeral=True)
            return
        await interaction.response.send_modal(RemoveQuestionModal(self))

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content="Application changes discarded.", view=ApplicationsSetupView(self.guild)
        )

    async def _save(self, interaction: discord.Interaction) -> None:
        title = self.title.strip()
        description = self.description.strip()
        if not title or not description:
            await interaction.response.send_message(
                "Set a title and a description first.", ephemeral=True
            )
            return
        if self.min_role_id is None or self.guild.get_role(self.min_role_id) is None:
            await interaction.response.send_message(
                "Pick the role required to apply.", ephemeral=True
            )
            return
        if not self.questions:
            await interaction.response.send_message(
                "Add at least one question.", ephemeral=True
            )
            return
        post = (
            self.guild.get_channel(config["applications_post_channel_id"])
            if config["applications_post_channel_id"]
            else None
        )
        if post is None:
            await interaction.response.send_message(
                "The post channel is missing — save the channels again.", ephemeral=True
            )
            return
        await interaction.response.defer()
        if self.app_id is None:
            app_id = uuid.uuid4().hex[:8]
            app = {
                "title": title,
                "description": description,
                "min_role_id": self.min_role_id,
                "questions": copy.deepcopy(self.questions),
                "panel_message_id": None,
            }
            config["applications"][app_id] = app
            save()
            try:
                await _post_panel(post, app_id, app)
            except discord.HTTPException:
                config["applications"].pop(app_id, None)
                save()
                await interaction.edit_original_response(
                    content=(
                        "Couldn't post the application panel — check my permissions in "
                        "that channel."
                    ),
                    view=ApplicationsSetupView(self.guild),
                )
                return
            interaction.client.add_view(ApplicationApplyView(app_id))
            save()
            await interaction.edit_original_response(
                content=f"Application **{title[:100]}** created.",
                view=ApplicationsSetupView(self.guild),
            )
            return
        app = config["applications"].get(self.app_id)
        if app is None:
            await interaction.edit_original_response(
                content="That application no longer exists.",
                view=ApplicationsSetupView(self.guild),
            )
            return
        app.update(
            {
                "title": title,
                "description": description,
                "min_role_id": self.min_role_id,
                "questions": copy.deepcopy(self.questions),
            }
        )
        save()
        note = ""
        if app.get("panel_message_id"):
            try:
                message = await post.fetch_message(app["panel_message_id"])
                await message.edit(embed=build_panel_embed(self.guild, app))
            except discord.NotFound:
                try:
                    await _post_panel(post, self.app_id, app)
                    save()
                except discord.HTTPException:
                    note = "\nThe panel couldn't be reposted — check my permissions."
            except discord.HTTPException:
                note = "\nThe panel couldn't be updated — check my permissions."
        else:
            try:
                await _post_panel(post, self.app_id, app)
                save()
            except discord.HTTPException:
                note = "\nThe panel couldn't be posted — check my permissions."
        await interaction.edit_original_response(
            content=f"Application **{title[:100]}** saved.{note}",
            view=ApplicationsSetupView(self.guild),
        )


class MetadataModal(discord.ui.Modal, title="Application details"):
    def __init__(self, builder: ApplicationBuilderView):
        super().__init__()
        self.builder = builder
        self.title_input = discord.ui.TextInput(
            label="Title", max_length=100, default=builder.title or None
        )
        self.description_input = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            max_length=2000,
            default=builder.description or None,
        )
        self.add_item(self.title_input)
        self.add_item(self.description_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        title = self.title_input.value.strip()
        if not title:
            await interaction.response.send_message("The title can't be empty.", ephemeral=True)
            return
        self.builder.title = title
        self.builder.description = self.description_input.value.strip()
        await interaction.response.edit_message(content=self.builder.summary(), view=self.builder)


class QuestionModal(discord.ui.Modal, title="Add question"):
    def __init__(self, builder: ApplicationBuilderView):
        super().__init__()
        self.builder = builder
        self.question_input = discord.ui.TextInput(label="Question", max_length=500)
        self.options_input = discord.ui.TextInput(
            label="Choices (one per line, optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=2000,
            placeholder="Leave empty for a text answer",
        )
        self.add_item(self.question_input)
        self.add_item(self.options_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text = self.question_input.value.strip()
        if not text:
            await interaction.response.send_message(
                "The question can't be empty.", ephemeral=True
            )
            return
        options, error = parse_options(self.options_input.value)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        self.builder.questions.append({"text": text, "options": options})
        self.builder.remove_button.disabled = False
        await interaction.response.edit_message(content=self.builder.summary(), view=self.builder)


class RemoveQuestionModal(discord.ui.Modal, title="Remove question"):
    def __init__(self, builder: ApplicationBuilderView):
        super().__init__()
        self.builder = builder
        self.number_input = discord.ui.TextInput(
            label=f"Question number (1-{len(builder.questions)})", max_length=3
        )
        self.add_item(self.number_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            number = int(self.number_input.value.strip())
        except ValueError:
            number = 0
        if not 1 <= number <= len(self.builder.questions):
            await interaction.response.send_message(
                "That question number doesn't exist.", ephemeral=True
            )
            return
        self.builder.questions.pop(number - 1)
        self.builder.remove_button.disabled = not self.builder.questions
        await interaction.response.edit_message(content=self.builder.summary(), view=self.builder)


class ManageApplicationsView(AdminOnlyView):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=1800)
        self.guild = guild
        options = [
            discord.SelectOption(
                label=app["title"][:100] or "Untitled",
                value=app_id,
                description=f"{len(app['questions'])} questions"[:100],
            )
            for app_id, app in list(config["applications"].items())[:25]
        ]
        select = discord.ui.Select(placeholder="Pick an application", options=options)
        select.callback = self._picked
        self.add_item(select)
        back = discord.ui.Button(label="← Back", style=discord.ButtonStyle.grey, row=1)
        back.callback = self._back
        self.add_item(back)

    async def _picked(self, interaction: discord.Interaction) -> None:
        app_id = interaction.data["values"][0]
        app = config["applications"].get(app_id)
        if app is None:
            await interaction.response.send_message(
                "That application no longer exists.", ephemeral=True
            )
            return
        await interaction.response.edit_message(
            content=application_details(self.guild, app_id, app),
            view=ApplicationManageView(self.guild, app_id),
        )

    async def _back(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content="**Applications setup** — channels, panels and active applications.",
            view=ApplicationsSetupView(self.guild),
        )


class ApplicationManageView(AdminOnlyView):
    def __init__(self, guild: discord.Guild, app_id: str):
        super().__init__(timeout=1800)
        self.guild = guild
        self.app_id = app_id
        for label, style, callback in (
            ("Edit", discord.ButtonStyle.blurple, self._edit),
            ("Remove", discord.ButtonStyle.danger, self._remove),
            ("← Back", discord.ButtonStyle.grey, self._back),
        ):
            button = discord.ui.Button(label=label, style=style)
            button.callback = callback
            self.add_item(button)

    async def _edit(self, interaction: discord.Interaction) -> None:
        app = config["applications"].get(self.app_id)
        if app is None:
            await interaction.response.send_message(
                "That application no longer exists.", ephemeral=True
            )
            return
        builder = ApplicationBuilderView(self.guild, existing=(self.app_id, app))
        await interaction.response.edit_message(content=builder.summary(), view=builder)

    async def _remove(self, interaction: discord.Interaction) -> None:
        app = config["applications"].get(self.app_id)
        if app is None:
            await interaction.response.send_message(
                "That application no longer exists.", ephemeral=True
            )
            return
        await interaction.response.edit_message(
            content=(
                f"Remove **{app['title']}**? Its panel is deleted and existing submissions "
                "stay in their threads. This can't be undone."
            ),
            view=RemoveApplicationView(self.guild, self.app_id),
        )

    async def _back(self, interaction: discord.Interaction) -> None:
        if not config["applications"]:
            await interaction.response.edit_message(
                content="No applications left.",
                view=ApplicationsSetupView(self.guild),
            )
            return
        await interaction.response.edit_message(
            content="**Manage applications** — pick one to edit or remove.",
            view=ManageApplicationsView(self.guild),
        )


class RemoveApplicationView(AdminOnlyView):
    def __init__(self, guild: discord.Guild, app_id: str):
        super().__init__(timeout=1800)
        self.guild = guild
        self.app_id = app_id
        for label, style, callback in (
            ("Remove application", discord.ButtonStyle.danger, self._remove),
            ("Cancel", discord.ButtonStyle.grey, self._cancel),
        ):
            button = discord.ui.Button(label=label, style=style)
            button.callback = callback
            self.add_item(button)

    async def _remove(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        app = config["applications"].pop(self.app_id, None)
        if app is None:
            await interaction.edit_original_response(
                content="That application no longer exists.",
                view=ApplicationsSetupView(self.guild),
            )
            return
        save()
        note = ""
        post = (
            self.guild.get_channel(config["applications_post_channel_id"])
            if config["applications_post_channel_id"]
            else None
        )
        if post is not None and app.get("panel_message_id"):
            try:
                message = await post.fetch_message(app["panel_message_id"])
                await message.delete()
            except discord.HTTPException:
                note = "\nI couldn't delete its panel message."
        await interaction.edit_original_response(
            content=f"Removed **{app['title']}**.{note}",
            view=ApplicationsSetupView(self.guild),
        )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        app = config["applications"].get(self.app_id)
        if app is None:
            await interaction.response.edit_message(
                content="That application no longer exists.",
                view=ApplicationsSetupView(self.guild),
            )
            return
        await interaction.response.edit_message(
            content=application_details(self.guild, self.app_id, app),
            view=ApplicationManageView(self.guild, self.app_id),
        )


async def _post_panel(channel: discord.TextChannel, app_id: str, app: dict) -> discord.Message:
    message = await channel.send(
        embed=build_panel_embed(channel.guild, app),
        view=ApplicationApplyView(app_id),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    app["panel_message_id"] = message.id
    return message
