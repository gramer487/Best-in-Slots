# LFG Discord Bot

A single-server Discord bot that posts two panels in one channel:

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

## Setup

1. Create an application at https://discord.com/developers/applications → **Bot** →
   **Reset Token** and copy the token. On the same page, under **Privileged Gateway
   Intents**, turn on **Message Content Intent** — the bot needs it to read `$close`
   in threads.
2. Invite the bot with this URL (replace `CLIENT_ID` with your application ID):
   ```
   https://discord.com/api/oauth2/authorize?client_id=CLIENT_ID&permissions=51808136192&scope=bot%20applications.commands
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
7. In your server, run `/setup` (Administrator permission or the configured admin role):
   - **Page 1:** pick the LFG channel, the roles LFG posts are allowed to ping, and an
     optional admin role that can run `/setup`.
   - **Page 2:** pick the roles for the three self-role buttons (leave empty to hide a
     button), then **Save & post panels**.

Re-running `/setup` deletes the old panels and posts fresh ones.

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
- `worldstate.py` — browse.wf world state fetcher (arbitration schedule + fissures).
- `config.py` — JSON config load/save (`config.json`).
- `config.json` — created on first save; holds channel/message/role IDs and thread
  timestamps. Managed by the bot and git-ignored: after a fresh clone it doesn't exist,
  so run `/setup` and it is recreated.

## Troubleshooting

- **"I don't have permission…" errors** → check the bot has Manage Roles / Manage Threads /
  Send Messages in the channel, and that its highest role is above the roles it assigns.
- **`/setup` doesn't appear** → the invite URL must include `applications.commands`
  (step 2). Registration can take a few minutes.
- **Panels were deleted** → re-run `/setup` to post them again.
