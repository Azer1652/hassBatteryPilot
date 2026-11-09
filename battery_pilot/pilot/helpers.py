"""The two switches the user controls, created as normal HA helpers.

They are UI helpers (Settings > Devices & services > Helpers), created over
the websocket API: no YAML, no configuration.yaml change, no restart, and
their state survives restarts.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

CONTROL = "input_boolean.battery_pilot_control"
CURTAIL = "input_boolean.battery_pilot_block_negative_export"
HELPERS = (
    (CONTROL, "Battery Pilot control", "mdi:battery-sync"),
    (CURTAIL, "Battery Pilot block negative export", "mdi:transmission-tower-off"),
)


async def ensure_helpers(ha, states: dict) -> list[str]:
    """Create missing helpers. Returns log lines."""
    missing = [h for h in HELPERS if h[0] not in states]
    if not missing:
        return []
    existing = (await ha.ws({"type": "input_boolean/list"}))[0] or []
    names = {i.get("name") for i in existing}
    todo = [h for h in missing if h[1] not in names]
    if todo:
        await ha.ws(*({"type": "input_boolean/create", "name": n, "icon": icon} for _, n, icon in todo))
    return [f"  created helper {eid} ({name}), off" for eid, name, _ in todo]
