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
    "button_roles": [None, None, None],  # roles behind the 3 self-role buttons
    "ping_roles": [],  # roles LFG posts are allowed to ping
    "admin_role_id": None,  # role allowed to run /setup (in addition to server admins)
    "threads": {},  # str(thread_id) -> ISO creation timestamp, for the 1h expiry
}

config = copy.deepcopy(_DEFAULTS)
if CONFIG_PATH.exists():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config.update(json.load(f))


def save() -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
