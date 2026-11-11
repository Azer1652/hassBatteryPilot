"""Find the user's entities by name, unless the options name them.

Pure functions over the `/api/states` list, so they are easy to test.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Options

# key: (domain, search patterns in order, required, is a power sensor)
ENTITIES = {
    "grid_import_power": ("sensor", [r"grid_import_power", r"import_power"], True, True),
    "grid_export_power": ("sensor", [r"grid_export_power", r"(?<!limit_)export_power$"], False, True),
    "house_load_power": ("sensor", [r"household_load_power", r"house(hold)?_load", r"load_power"], True, True),
    "pv_power": ("sensor", [r"total_pv_power", r"pv_power$"], False, True),
    "battery_soc": ("sensor", [r"battery_soc$", r"battery_soc"], True, False),
    "battery_voltage": ("sensor", [r"battery_voltage$", r"battery_voltage"], False, False),
    "tou_switch": ("switch", [r"time_of_use$", r"time_of_use"], True, False),
    "grid_charge_switch": ("switch", [r"allow_grid.*charg", r"grid_charg"], True, False),
    "charge_current": ("number", [r"(?<!dis)charge_current"], True, False),
    "discharge_current": ("number", [r"discharge_current"], True, False),
    "charge_start": ("time", [r"(?<!dis)charge_start.*slot_1$"], True, False),
    "charge_end": ("time", [r"(?<!dis)charge_end.*slot_1$"], True, False),
    "discharge_start": ("time", [r"discharge_start.*slot_1$"], True, False),
    "discharge_end": ("time", [r"discharge_end.*slot_1$"], True, False),
    "export_limit": ("number", [r"export_power_limit", r"export.*limit"], False, False),
    "peak_shaving_switch": ("switch", [r"peak_shaving_mode$", r"peak_shaving"], False, False),
    "peak_shaving_limit": ("number", [r"peak_max_usable_grid_power", r"max_usable_grid_power"], False, False),
    "peak_shaving_soc": ("number", [r"peak_baseline_soc", r"baseline_soc"], False, False),
    "feed_in_priority_switch": ("switch", [r"feed_in_priority(_mode)?$", r"feed_in_priority"], False, False),
    "forecast_power_now": ("sensor", [r"^power_production_now$", r"power_production_now"], False, False),
    "forecast_today": ("sensor", [r"^energy_production_today$"], False, False),
    "forecast_tomorrow": ("sensor", [r"^energy_production_tomorrow$"], False, False),
    "price_sensor": ("sensor", [r"current_electricity_market_price", r"^nordpool", r"electricity_price"], False, False),
    # never guessed: only the entity named in the settings
    "car_charger_power": ("sensor", [], False, True),
}
TIME_SLOT_KEYS = ("charge_start", "charge_end", "discharge_start", "discharge_end")

# What each optional entity is needed for (shown in the log when it is missing).
NEEDED_FOR = {
    "grid_export_power": "the contract comparison (sold kWh)",
    "pv_power": "learning tree shade",
    "battery_voltage": "amps to kW (the battery_voltage option is used without it)",
    "export_limit": "blocking export at negative prices",
    "peak_shaving_switch": "peak shaving: keeping the battery for a dearer hour while the grid feeds the house",
    "peak_shaving_limit": "setting the peak-shaving power to the import limit (the inverter's own value is used without it)",
    "feed_in_priority_switch": "solar hold: exporting morning surplus to keep room in the battery for midday",
    "peak_shaving_soc": "setting the peak-shaving battery level (the inverter's own value is used without it)",
    "forecast_power_now": "learning tree shade",
    "forecast_today": "the solar forecast when HA's energy forecast is not set up",
    "forecast_tomorrow": "the solar forecast when HA's energy forecast is not set up",
    "price_sensor": "dynamic prices and the contract comparison",
}


@dataclass
class Found:
    entity_id: str | None
    how: str
    scale: float = 1.0      # power sensors: multiply by this to get kW
    unit: str = ""


def pick_slot_set(found: list[str], slot_set: str) -> list[str]:
    if slot_set == "time_charging":
        narrowed = [e for e in found if "time_charging" in e]
    else:
        narrowed = [e for e in found if "time_charging" not in e]
    return narrowed or found


def power_scale(entity_id: str, states: dict) -> tuple[float, str]:
    unit = ((states.get(entity_id) or {}).get("attributes") or {}).get("unit_of_measurement") or ""
    if unit == "kW":
        return 1.0, unit
    if unit == "MW":
        return 1000.0, unit
    return 0.001, unit or "no unit, assumed W"


def resolve(key: str, opts: Options, states: dict) -> Found:
    domain, patterns, _required, is_power = ENTITIES[key]
    chosen = opts.entities.get(key, "")
    if chosen:
        if chosen not in states:
            return Found(None, f"set to {chosen}, but Home Assistant has no entity with that ID")
        found = Found(chosen, "set in the app settings")
    else:
        found = Found(None, "not found")
        ids = sorted(e for e in states if e.startswith(domain + "."))
        for p in patterns:
            hits = [e for e in ids if re.search(p, e.split(".", 1)[1])]
            if key in TIME_SLOT_KEYS or key in ("charge_current", "discharge_current"):
                hits = pick_slot_set(hits, opts.slot_entities)
            if len(hits) == 1:
                found = Found(hits[0], "found automatically")
                break
            if len(hits) > 1:
                solis = [e for e in hits if "solis" in e]
                if len(solis) == 1:
                    found = Found(solis[0], "found automatically")
                else:
                    found = Found(None, "several candidates, pick one in the settings: " + ", ".join(hits[:6]))
                break
    if found.entity_id and is_power:
        found.scale, found.unit = power_scale(found.entity_id, states)
    return found


def resolve_all(opts: Options, states: dict) -> tuple[dict[str, Found], list[str]]:
    """All entities, plus the keys of required ones that are missing."""
    out = {k: resolve(k, opts, states) for k in ENTITIES}
    missing = [k for k, f in out.items() if f.entity_id is None and ENTITIES[k][2]]
    return out, missing


def find_notify(opts: Options, services: list) -> tuple[str | None, str]:
    """Notify action for phone alerts: the option, else the only Companion app phone."""
    chosen = (opts.notify_service or "").strip()
    if chosen:
        return (chosen if chosen.startswith("notify.") else "notify." + chosen), "set in the app settings"
    phones: list[str] = []
    for dom in services or []:
        if dom.get("domain") == "notify":
            phones = sorted(s for s in (dom.get("services") or {}) if s.startswith("mobile_app_"))
    if len(phones) == 1:
        return "notify." + phones[0], "found automatically"
    if len(phones) > 1:
        return None, "several phones found (" + ", ".join(phones) + "): set notify_service to pick one"
    return None, "no Companion app found: alerts go to HA notifications only"
