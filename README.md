# Best in Slots Bot

A single-server Discord bot with LFG panels, a world state embed and modmail.

1. **Self roles** — 3 buttons that toggle roles on/off. Users can pick more than one.
2. **LFG** — a "Start LFG" button opens an ephemeral form (role to ping, players needed,
   duration, optional note via a modal). Submitting posts the LFG with a role ping and opens
   a thread. Threads are deleted exactly 1 hour after creation. The thread owner
   can type `$close` inside their thread to have it deleted after 10 minutes.
3. **World state** — a third embed below the panels shows the current arbitration (mission,
   location, tier, reset time) and whether a Steel Path Void Cascade fissure is live.
   Data comes from browse.wf and the embed refreshes the moment the arbitration rotates
   or a live cascade expires (at least every 10 minutes).

Any non-bot message posted directly in the LFG channel is deleted — conversation happens
in threads. On every restart the bot clears the whole LFG channel and reposts the panels.

### Modmail

Members contact staff with the panel button or by DMing the bot directly. Each user gets a
private channel in a category only the moderator role can see; its first message is an info
card with the user's ID, join date, account age, top 15 roles and links to their past
transcripts. Every message they send the bot is relayed there as an embed. Moderators reply with `&reply <message>` (or `&r <message>`)
and close the conversation with `&close` or the **Close** button, which posts a transcript
(usernames only, no pings) to the configured log forum and deletes the channel. Setup fields:
panel channel (optional), roles pinged for new modmail, the moderator role, and the log forum
(optional).

## Setup

1. Create an application at https://discord.com/developers/applications → **Bot** →
   **Reset Token** and copy the token. On the same page, under **Privileged Gateway
   Intents**, turn on **Message Content Intent** (the bot needs it to read `$close`
   in threads and `&reply`/`&close` in modmail) and **Server Members Intent** (needed
   for modmail member lookups).
2. Invite the bot with this URL (replace `CLIENT_ID` with your application ID):
   ```
   https://discord.com/api/oauth2/authorize?client_id=CLIENT_ID&permissions=51808136208&scope=bot%20applications.commands
   ```
3. Install Python 3.8+.
4. Create a virtual environment and install the dependencies.

   Windows:
   ```
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```
   Linux:
   ```
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
   (On Debian/Ubuntu, if venv creation complains about pip, `sudo apt install python3-venv`
   first. Note: on Linux the venv has a `bin/` directory, not `Scripts/`.)
5. Put your token in the `.env` file next to `main.py`:
   ```
   DISCORD_TOKEN=your-token-here
   ```
6. Run the bot.

   Windows: `.venv\Scripts\python main.py`
   Linux (venv activated): `python3 main.py`
7. In your server, run `/setup` (Administrator permission or the configured admin role)
   and pick **LFG** or **Modmail**.
   - **LFG page 1:** pick the LFG channel, the roles LFG posts are allowed to ping, and an
     optional admin role that can run `/setup`.
   - **LFG page 2:** pick the roles for the three self-role buttons (leave empty to hide a
     button), then **Save & post panels**.
   - **Modmail:** pick the panel channel (optional), the roles pinged for new modmail, the
     moderator role, and the log forum (optional), then **Save modmail setup**. This creates
     the staff-only **Modmail** category and posts the contact panel.

Re-running the LFG setup deletes the old panels and posts fresh ones.

## Recommended channel permissions

For the LFG channel, set `@everyone` to:

- View Channel ✅
- Send Messages ❌
- Send Messages in Threads ✅
- Create Public/Private Threads ❌

The bot deletes stray messages anyway, but this keeps the channel clean without relying
on it.

## Files

- `main.py` — bot wiring, thread expiry loop, message cleanup, `/setup` command.
- `views.py` — all UI: role buttons, LFG form + note modal, setup pages, status embed.
- `modmail.py` — modmail panel, category/channel creation, DM relay, `&reply`/`&close`,
  transcript logging.
- `worldstate.py` — browse.wf world state fetcher (arbitration schedule + fissures).
- `config.py` — JSON config load/save (`config.json`).
- `config.json` — created on first save; holds channel/message/role IDs and thread
  timestamps. Managed by the bot and git-ignored: after a fresh clone it doesn't exist,
  so run `/setup` and it is recreated.

## Troubleshooting

- **"I don't have permission…" errors** → check the bot has Manage Roles / Manage Threads /
  Manage Channels / Send Messages in the channel, and that its highest role is above the
  roles it assigns. If the bot was invited before modmail existed, re-invite it with the
  URL from step 2 to grant Manage Channels.
- **Modmail replies not reaching users** → users who block the bot or disable DMs can't
  receive replies; the modmail channel shows a warning instead.
- **`/setup` doesn't appear** → the invite URL must include `applications.commands`
  (step 2). Registration can take a few minutes.
- **Panels were deleted** → re-run `/setup` to post them again.
