"""JSON config store for the LFG bot. Shared module-level `config` dict."""

import copy
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("config.json")

_DEFAULTS = {
    "guild_id": None,
    "channel_id": None,
    "panel_message_id": None,
    "lfg_message_id": None,
    "status_message_id": None,  # world state embed (arbitration + SP cascade flag)
    "button_roles": [None, None, None],  # roles behind the 3 self-role buttons
    "ping_roles": [],  # roles LFG posts are allowed to ping
    "admin_role_id": None,  # role allowed to run /setup (in addition to server admins)
    "threads": {},  # str(thread_id) -> {"created": iso, "host": user_id, "delete_at": iso?}
    "suggestion_channel_id": None,  # channel that receives /suggestion embeds
    "suggestion_ping_role_id": None,  # role pinged for each suggestion
    "suggestions": [],  # internal weapon ids already suggested (one per weapon)
    "modmail_embed_channel_id": None,  # where the "contact a moderator" panel is posted
    "modmail_ping_roles": [],  # roles pinged when a new modmail opens
    "modmail_mod_role_id": None,  # role that sees the modmail category and can reply
    "modmail_log_channel_id": None,  # forum channel that receives closed-mail transcripts
    "modmail_category_id": None,  # staff-only category holding the modmail channels
    "modmail_panel_message_id": None,
    "modmails": {},  # str(user_id) -> modmail channel id
    "modmail_closes": {},  # str(channel_id) -> {"user_id", "close_at": iso, "warned": bool}
    "modmail_alerts": {},  # str(modmail_channel_id) -> [moderator ids pinged on user replies]
    "modmail_history": {},  # str(user_id) -> [{"thread": forum thread id, "title": ...}]
    "applications_post_channel_id": None,  # public channel holding the Apply panels
    "applications_review_channel_id": None,  # staff channel receiving submissions + threads
    "applications": {},  # str(app_id) -> app definition + panel_message_id
    "application_submissions": {},  # str(thread_id) -> submission record + status
}

config = copy.deepcopy(_DEFAULTS)
if CONFIG_PATH.exists():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config.update(json.load(f))


def save() -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
