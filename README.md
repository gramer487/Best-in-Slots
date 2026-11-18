# Best in Slots Bot

Best in Slots Bot is a private Discord bot maintained for the Best in Slots community. It
provides the server's day-to-day tools — self roles, LFG, live world state, weapon
suggestions, staff applications, tags, personal voice channels, giveaways and
watermarking — without anything leaving Discord. It is not publicly distributed or
self-hostable; this document describes what it does and how members and staff use it.

## Self roles

A panel of buttons, one per configured role and up to 25 per panel, lets members give
themselves roles and remove them again by pressing the same button. Members can hold
several at once.

## LFG

A **Start LFG** button opens a short form: the role to ping, how many players are needed,
the duration, the region and an optional note. Submitting posts the LFG with its role
ping, opens a thread and adds an owner-only control panel to it for increasing or
decreasing the player count, marking the group full and closing it. The role list pages
through any number of pingable roles when there are more than 25.

The owner marks players by reacting ✅ to that player's message: each mark fills one
needed slot and adds the player to the public embed, and removing the reaction undoes it.
`$add @user` and `$remove @user` do the same from the thread, and `$close` (or **Close
thread**) schedules deletion in 10 minutes. Posts expire 2 hours after creation; the owner
can change this with **Set expiry**, from 30 minutes up to 14 days. When a post expires or
is closed, its thread and post are removed. The LFG channel stays clean — anything posted
there that isn't the bot's own is deleted, and conversation happens in threads.

## World state

An embed below the panels shows the current arbitration — mission, location, tier and
reset time — and whether a Steel Path Void Cascade fissure is live. It refreshes the
moment the arbitration rotates or a live cascade expires, and at least every 10 minutes.

## Suggestions

`/suggestion` autocompletes weapon names and posts an embed titled with the weapon (plus
the optional note) to the configured channel, pinging the configured role. Each weapon
can only be suggested once.

## Applications

Staff build applications in `/setup`: title, description, the role required to apply, the
channel that receives submissions, roles to ping when one arrives, and any number of
free-text or multiple-choice questions. Each active application gets its own embed with an
**Apply** button in the post channel.

Applying starts a DM conversation: the bot asks the questions one at a time (multiple
choice uses a dropdown), shows a summary and asks for confirmation, then posts the answers
to that application's submission channel and pings its roles. Images sent in the DM are
refused with a reply asking for a link instead. The submission also gets a private review
thread containing the same embed and **Approve**/**Deny** buttons; the ✅/❌ reactions on
the post are for staff discussion and are not connected to the decision. Only the reviewer
role picked in setup (or administrators) can decide. Denying opens a modal for a reason,
which the applicant receives by DM as an anonymous note "from staff" and which is also
shown in the thread embed. A decision updates the thread embed with the outcome and who
decided, removes the submission post, DMs the applicant, and locks and archives the
thread, which keeps its history.

## Tags

`/tag create name:test` stores a message behind `.test`. Sending a message that starts
with `.test` makes the bot post it, and `/tag use name:test` posts it from a slash command
with an optional channel and ping. Only moderators — the configured moderator role, or
administrators — can create tags.

`/tag manage name:test` shows a tag's content, creator, creation date and use count with
**Ping mentions**, **Edit**, **Rename** and **Remove** buttons, while `/tag list` lists
every tag. Names are case-insensitive, 1–32 characters, and limited to letters, numbers,
`-` and `_`. If a tag's text contains role or user mentions, creation asks whether those
mentions should ping when the tag is used; the answer is saved with the tag and can be
flipped later with **Ping mentions**.

`.test -c <channel id>` posts a tag in another channel (moderators and administrators
only), and `.test -u <user id>` pings that user along with the tag. `/tag use` accepts the
same options as `channel:` and `mention:`. Tags work in normal channels and threads, not
in DMs or modmail channels, and the `.` must be the first character of the message. Tag
creations, edits, renames and removals are recorded in a staff log channel; uses are not.

## Voice hub

Joining the configured hub voice channel creates a personal channel named
`<username>'s voice chat` with the hub's permissions, moves the member into it and posts
an owner-only control panel in its text chat: **Lock/Unlock** (blocks new joins), **Set
limit** (0–99 users, 0 removes the limit) and **Hide/Unhide**. Lock and Hide deny the role
chosen in setup, so the owner keeps access. A channel is deleted once everyone leaves it.
An optional log channel keeps one embed per created channel, edited with every creation
and button press.

## Giveaways

Administrators set the giveaway channel, log channel, ping role, manager role and
optional blacklist role in `/setup` → **Giveaways**. Every creation is logged in the log
channel with its title, host, value, winner count, end time, channel, who created it and a
jump link, along with the giveaway's image.

`/giveaway create` takes a title, duration (`30s`, `30m`, `2h`, `7d`), winner count, host,
and a value (`150` or `150p` — the `p` is normalized away), plus an optional image
attachment and channel override. It posts a gold embed showing the title, host, winners,
entries and end time (as a Discord timestamp, so it counts down live) with the attached
image as the main embed image, an **Enter giveaway** button, and pings the setup role.
Members with the blacklist role can't enter. Entry handling is built for bursts — hundreds
of entries in a few seconds are fine.

When the duration runs out — or a manager presses **End now** in `/giveaway manage` — the
embed turns red, lists the winners and DMs them, and the panel shows **Reroll winners** and
**Mark as done**. Reroll draws replacements from entrants who haven't won yet; marking
done awards the value to the host as points and re-checks their role tier. Ending a
giveaway early and marking it done also edit the log entry to show who did it.

`/gwinfo [user]` shows a user's points and lists their giveaways, and administrators can
revoke or restore the points of a completed giveaway there. `/gwroles` sets three roles
and the points each needs; hosts automatically receive the highest tier they qualify for
when their points change.

## Watermark

`/watermark` takes an image and returns a preview with "This roll data relates to a
specific build and may not be the best for your build" in gold with a black outline on a
white square that hugs the bottom-right corner, plus a large gray diagonal
"discord.gg/bestinslots" watermark at 30% opacity, rotated to run corner to corner and
sized as large as the image allows. The layout scales with the image's dimensions.

## Modmail

Members contact staff with the panel button or by DMing the bot directly. Each user gets a
private channel in a category only the moderator role can see; its first message is an
info card with the user's ID, join date, account age, top 15 roles and links to their past
transcripts. Every message they send the bot is relayed there as an embed; attachments
(screenshots etc.) are copied along with it into the transcript thread. If the member
leaves the server while a modmail is open, staff are told in the channel that the user's
messages can no longer be relayed.

Moderators reply with `&reply <message>` (or `&r <message>`); `&edit <new text>` rewrites
the last reply, both in the channel and in the user's DMs. `&close` or the **Close** button
closes the conversation, posting a transcript (usernames only, no pings) to the configured
log forum and deleting the channel. `&close 10m` or `&close 1h` schedules the close
instead: the opener is DM'd 10 minutes before it happens and the warning is posted in the
modmail channel too, and a reply from either side cancels it — staff chatter, `&edit`,
`&alert` and `&id` don't. While the user is typing in the DMs, the modmail channel shows
the bot's typing indicator. `&alert` toggles a ping for the moderator
who types it whenever the user replies in that mail. `&id` prints the opener's ID in a
copyable code block, and `/openthread <userid>` (moderators only) opens a modmail from the
staff side and DMs the user without pinging the modmail ping roles; pass `ephemeral: false`
to make the result visible to the channel.

## Administration

Everything is configured from inside Discord. `/setup` is limited to administrators and
the configured admin role and covers the LFG panel, modmail, suggestions, applications and
their reviewer role, the voice hub, and giveaways — including the giveaway channel, ping
role, manager role, log channel and blacklist role. `/gwroles` configures the three
giveaway host tiers, `/gwinfo` reviews a host's points, and `/giveaway manage` ends a
running giveaway early. `/presence` changes what the bot is shown doing — playing,
watching, listening or competing — live and across restarts.

For roles to be handed out, the bot's own role must sit above them in the role hierarchy —
this applies to self roles, the roles used by the voice hub, and the giveaway tiers.
Applicants, modmail users and giveaway winners also need to accept DMs from the server.
