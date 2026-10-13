"""Live Warframe world state from browse.wf: arbitration schedule + active fissures.

browse.wf/live renders its widgets from these sources (same ones we use):
- https://oracle.browse.wf/worldState.min.json  -> ActiveMissions (fissures; Hard=true is Steel Path)
- https://browse.wf/arbys.txt                  -> precomputed hourly arbitration schedule
- https://browse.wf/warframe-public-export-plus/ExportRegions.json -> node id -> Lotus path keys
- https://browse.wf/warframe-public-export-plus/dict.en.json      -> Lotus path -> localized text
- https://browse.wf/supplemental-data/arbyTiers.js                -> node id -> arbitration tier letter
"""

from __future__ import annotations

import datetime as dt
import json
import re

import aiohttp

WORLD_STATE_URL = "https://oracle.browse.wf/worldState.min.json"
ARBYS_URL = "https://browse.wf/arbys.txt"
REGIONS_URL = "https://browse.wf/warframe-public-export-plus/ExportRegions.json"
DICT_URL = "https://browse.wf/warframe-public-export-plus/dict.en.json"
TIERS_URL = "https://browse.wf/supplemental-data/arbyTiers.js"

CASCADE = "MT_VOID_CASCADE"

# VoidT modifiers -> fissure tier names.
_TIERS = {
    "VoidT1": "Lith",
    "VoidT2": "Meso",
    "VoidT3": "Neo",
    "VoidT4": "Axi",
    "VoidT5": "Requiem",
    "VoidT6": "Omnia",
}

_regions: dict | None = None  # node id -> {name, systemName, missionName} (Lotus paths)
_dict: dict | None = None  # Lotus path -> localized string
_arbys: dict | None = None  # hour timestamp -> node id
_tiers: dict | None = None  # node id -> arbitration tier letter


def _display(lotus_path) -> str:
    """'/Lotus/Language/Locations/Sinai' -> 'Sinai'."""
    return lotus_path.rsplit("/", 1)[-1] if lotus_path else "?"


def localize(lotus_path) -> str:
    """Localize a Lotus path; fall back to its last segment."""
    if not lotus_path:
        return "?"
    value = (_dict or {}).get(lotus_path)
    return value if value else _display(lotus_path)


def _title(s: str) -> str:
    """'MOBILE DEFENSE' -> 'Mobile Defense'."""
    return " ".join(word.capitalize() for word in s.split())


def parse_tiers(js_text: str) -> dict:
    """window.arbyTiers = { SolNode450: "S", ... }; -> {node id: letter}."""
    body = js_text.split("=", 1)[1].rsplit(";", 1)[0]
    body = re.sub(r"([A-Za-z0-9_]+)\s*:", r'"\1":', body)
    body = re.sub(r",\s*}", "}", body)
    return json.loads(body)


def arbys_for_hour(text: str, hour_ts: int) -> str | None:
    """Node id of the arbitration for the hour starting at hour_ts, from arbys.txt."""
    for line in text.splitlines():
        if not line.strip():
            continue
        ts_s, node = line.split(",", 1)
        if int(ts_s) == hour_ts:
            return node
    return None


def find_sp_cascade(world_state: dict) -> dict | None:
    """First active Steel Path Void Cascade fissure (tier/node/expiry), or None."""
    for mission in world_state.get("ActiveMissions", []):
        if mission.get("MissionType") == CASCADE and mission.get("Hard"):
            expiry = mission["Expiry"]["$date"]["$numberLong"]
            return {
                "tier": _TIERS.get(mission.get("Modifier"), mission.get("Modifier")),
                "node": mission["Node"],
                "expiry": dt.datetime.fromtimestamp(int(expiry) / 1000, dt.timezone.utc),
            }
    return None


def node_info(node_id: str) -> dict:
    """{name, system, mission} for a node id, localized through the cached dict."""
    entry = (_regions or {}).get(node_id, {})
    return {
        "name": localize(entry.get("name")),
        "system": localize(entry.get("systemName")),
        "mission": _title(localize(entry.get("missionName"))),
    }


async def fetch_worldstate(session: aiohttp.ClientSession) -> dict:
    """Current arbitration + SP Void Cascade flag, with node names localized."""
    global _regions, _dict, _arbys, _tiers
    now = dt.datetime.now(dt.timezone.utc)
    hour_ts = int(now.replace(minute=0, second=0, microsecond=0).timestamp())

    if _arbys is None or hour_ts not in _arbys:
        # The schedule covers years and only the current hour matters — refetch hourly.
        async with session.get(ARBYS_URL) as resp:
            text = await resp.text()
        _arbys = {}
        for line in text.splitlines():
            if line.strip():
                ts_s, node = line.split(",", 1)
                _arbys[int(ts_s)] = node
    arb_node = _arbys.get(hour_ts)

    async with session.get(WORLD_STATE_URL) as resp:
        world_state = await resp.json()

    sp = find_sp_cascade(world_state)

    nodes_needed = [n for n in (arb_node, sp["node"] if sp else None) if n]
    if _regions is None or any(n not in _regions for n in nodes_needed):
        async with session.get(REGIONS_URL) as resp:
            _regions = await resp.json()
    paths_needed = []
    for n in nodes_needed:
        entry = _regions.get(n, {})
        paths_needed += [entry.get("name"), entry.get("systemName"), entry.get("missionName")]
    if _dict is None or any(p and p not in _dict for p in paths_needed):
        async with session.get(DICT_URL) as resp:
            _dict = await resp.json()
    if _tiers is None:
        try:
            async with session.get(TIERS_URL) as resp:
                _tiers = parse_tiers(await resp.text())
        except (aiohttp.ClientError, ValueError):
            _tiers = {}

    arbitration = None
    if arb_node is not None:
        info = node_info(arb_node)
        arbitration = {
            "name": info["name"],
            "system": info["system"],
            "mission": info["mission"],
            "tier": _tiers.get(arb_node, "?"),
            # Arbitration rotates at the next hour boundary.
            "rotates_at": dt.datetime.fromtimestamp(hour_ts + 3600, dt.timezone.utc),
        }
    if sp is not None:
        info = node_info(sp["node"])
        sp["name"] = info["name"]
        sp["system"] = info["system"]
    return {"arbitration": arbitration, "sp_cascade": sp}
