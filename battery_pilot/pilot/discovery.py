"""Find the user's entities by name, unless the options name them.

Pure functions over the `/api/states` list, so they are easy to test.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Options
from .marstek import JAAPP, LOCAL, Integration, registry_match
from .marstek import REGISTRY as MARSTEK_REGISTRY

# key: (domain, search patterns in order, required, is a power sensor)
ENTITIES = {
    # (the last pattern of each: the names of the Solax Modbus integration)
    "grid_import_power": ("sensor", [r"grid_import_power", r"import_power", r"solax.*_grid_import$"], True, True),
    "grid_export_power": ("sensor", [r"grid_export_power", r"(?<!limit_)export_power$", r"solax.*_grid_export$"],
                          False, True),
    "house_load_power": ("sensor", [r"household_load_power", r"solax.*_house_load$", r"house(hold)?_load",
                                      r"load_power"], True, True),
    "pv_power": ("sensor", [r"total_pv_power", r"pv_power$", r"solax.*_pv_power_total$"], False, True),
    # signed DC battery power (+ discharge); not the unsigned `battery_power`
    "battery_power": ("sensor", [r"battery_power_net$"], False, True),
    # the inverter's own reading at its grid connection, + to the grid (Solis "AC Grid Port Power")
    "inverter_grid_power": ("sensor", [r"ac_grid_port_power$"], False, True),
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
    "export_limit": ("number", [r"export_power_limit", r"export.*limit", r"export_control_user_limit"], False, False),
    "peak_shaving_switch": ("switch", [r"peak_shaving_mode$", r"peak_shaving"], False, False),
    "peak_shaving_limit": ("number", [r"peak_max_usable_grid_power", r"max_usable_grid_power"], False, False),
    "peak_shaving_soc": ("number", [r"peak_baseline_soc", r"baseline_soc"], False, False),
    "feed_in_priority_switch": ("switch", [r"feed_in_priority(_mode)?$", r"feed_in_priority"], False, False),
    "self_use_switch": ("switch", [r"self_use_mode$"], False, False),
    "forecast_power_now": ("sensor", [r"^power_production_now$", r"power_production_now"], False, False),
    "forecast_today": ("sensor", [r"^energy_production_today$"], False, False),
    "forecast_tomorrow": ("sensor", [r"^energy_production_tomorrow$"], False, False),
    # ENTSO-e keeps its price list on the "average" sensor (the "current" one had it in old versions)
    "price_sensor": ("sensor", [r"average_electricity_price$", r"current_electricity_market_price", r"^nordpool",
                                r"electricity_price"], False, False),
    # never guessed: only the entity named in the settings
    "car_charger_power": ("sensor", [], False, True),
    # A Marstek battery (battery_system: marstek): found in the entity registry (marstek.REGISTRY);
    # the patterns are the local integration's default names, for when the registry cannot be read.
    # one signed grid meter, + import (a HomeWizard P1 meter's Power): used before the two above
    "grid_power": ("sensor", [r"^p1_meter(_active)?_power$"], False, True),
    # the grid meter's own quarter-hour average and month peak (HomeWizard P1: Average demand, Peak demand)
    "quarter_average_power": ("sensor", [r"^p1_meter_average_demand$"], False, True),
    "month_peak_power": ("sensor", [r"^p1_meter_peak_demand_current_month$"], False, True),
    "marstek_auto_mode_button": ("button", [r"^marstek_.*auto_mode$"], False, False),
    "marstek_mode_select": ("select", [r"^marstek_.*operating_mode$"], False, False),
    "marstek_last_message": ("sensor", [r"^marstek_.*last_message_received$"], False, False),
    "battery_soc_backup": ("sensor", [], False, False),
    "battery_charge_power_backup": ("sensor", [], False, True),
    "battery_discharge_power_backup": ("sensor", [], False, True),
    "battery_report_time_backup": ("sensor", [], False, False),
}
# Default names of the local integration's sensors, for the keys a Solis has too.
MARSTEK_PATTERNS = {
    "battery_soc": [r"^marstek_[a-z0-9]+_state_of_charge$", r"^marstek_.*_battery_level$"],
    "battery_power": [r"^marstek_[a-z0-9]+_power$", r"^marstek_.*_battery_power$"],
    "battery_voltage": [r"^marstek_[a-z0-9]+_voltage$"],
}
# Solis-only entities: not looked for with a Marstek battery.
SOLIS_ONLY = ("inverter_grid_power", "tou_switch", "grid_charge_switch", "charge_current", "discharge_current", "charge_start",
              "charge_end", "discharge_start", "discharge_end", "peak_shaving_switch", "peak_shaving_limit",
              "peak_shaving_soc", "feed_in_priority_switch", "self_use_switch")
MARSTEK_ONLY = ("grid_power", "quarter_average_power", "month_peak_power", "marstek_auto_mode_button",
                "marstek_mode_select",
                "marstek_last_message", "battery_soc_backup",
                "battery_charge_power_backup", "battery_discharge_power_backup", "battery_report_time_backup")
TIME_SLOT_KEYS = ("charge_start", "charge_end", "discharge_start", "discharge_end")
# Only needed when charging goes through the time-of-use slots (not with the force action).
SLOT_ONLY = ("tou_switch", "grid_charge_switch", "charge_current", "discharge_current") + TIME_SLOT_KEYS
FORCE_DOMAIN = "solis_modbus"
FORCE_SERVICES = ("solis_force_battery_charge", "solis_force_battery_discharge", "solis_stop_force_charge_discharge")

# What each optional entity is needed for (shown in the log when it is missing).
NEEDED_FOR = {
    "grid_export_power": "the contract comparison (sold kWh)",
    "pv_power": "learning tree shade",
    "battery_power": "measuring the inverter's own use (the efficiency figure)",
    "inverter_grid_power": "reading the inverter's own use at its grid port (it is worked out from the meters without it)",
    "battery_voltage": "amps to kW (the battery_voltage option is used without it)",
    "export_limit": "blocking export at negative prices",
    "peak_shaving_switch": "peak shaving: keeping the battery for a dearer hour while the grid feeds the house",
    "peak_shaving_limit": "setting the peak-shaving power to the import limit (the inverter's own value is used without it)",
    "feed_in_priority_switch": "solar hold: exporting morning surplus to keep room in the battery for midday",
    "self_use_switch": "putting the inverter back in its Self-Use mode after peak shaving or solar hold",
    "peak_shaving_soc": "setting the peak-shaving battery level (the inverter's own value is used without it)",
    "forecast_power_now": "learning tree shade",
    "forecast_today": "the solar forecast when HA's energy forecast is not set up",
    "forecast_tomorrow": "the solar forecast when HA's energy forecast is not set up",
    "price_sensor": "dynamic prices and the contract comparison",
    "grid_power": "a fast and exact grid reading (a P1 meter) for the meter and the Marstek's peak shaving",
    "quarter_average_power": "the capacity peak: the meter's own quarter-hour average",
    "month_peak_power": "the capacity peak: the meter's own month peak",
    "battery_soc_backup": "the Marstek cloud as a backup when the local API is silent",
    "battery_charge_power_backup": "the Marstek cloud as a backup for the battery power",
    "battery_discharge_power_backup": "the Marstek cloud as a backup for the battery power",
    "battery_report_time_backup": "noticing that the Marstek cloud's figures are old",
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


def resolve(key: str, opts: Options, states: dict, registry: list | None = None, local: str = LOCAL) -> Found:
    domain, patterns, _required, is_power = ENTITIES[key]
    chosen = opts.entities.get(key, "")
    marstek = opts.marstek and key in MARSTEK_REGISTRY
    if opts.marstek and key in SOLIS_ONLY and not chosen:
        return Found(None, "not used with a Marstek battery")
    if not opts.marstek and key in MARSTEK_ONLY and not chosen:
        return Found(None, "only used with a Marstek battery")
    if marstek:
        patterns = MARSTEK_PATTERNS.get(key, patterns)
    hits = registry_match(key, registry, local) if marstek and not chosen and registry else []
    hits = [e["entity_id"] for e in hits if e.get("entity_id") in states]
    if chosen:
        if chosen not in states:
            return Found(None, f"set to {chosen}, but Home Assistant has no entity with that ID")
        found = Found(chosen, "set in the app settings")
    elif len(hits) == 1:
        found = Found(hits[0], "found in the Marstek integration")
    elif len(hits) > 1:
        found = Found(None, "several Marstek batteries, pick one in the settings: " + ", ".join(sorted(hits)[:6]))
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
                brand = [e for e in hits if "solis" in e] or [e for e in hits if "solax" in e]
                if len(brand) == 1:
                    found = Found(brand[0], "found automatically")
                else:
                    found = Found(None, "several candidates, pick one in the settings: " + ", ".join(hits[:6]))
                break
    if found.entity_id and is_power:
        found.scale, found.unit = power_scale(found.entity_id, states)
    return found


def resolve_all(opts: Options, states: dict, force: bool = False, registry: list | None = None,
                integration: Integration = JAAPP) -> tuple[dict[str, Found], list[str]]:
    """All entities, plus the keys of required ones that are missing (`force`: charging goes through
    the force action, so the time-of-use entities are optional; `registry`: Home Assistant's entity
    registry, where a Marstek battery's entities are found)."""
    out = {k: resolve(k, opts, states, registry, integration.domain) for k in ENTITIES}
    if opts.marstek:
        missing = [k for k in ("grid_import_power", "house_load_power", integration.auto_entity)
                   + (("marstek_last_message",) if integration.last_message else ())
                   if out[k].entity_id is None and not (k == "grid_import_power" and out["grid_power"].entity_id)
                   and not (k == "house_load_power" and out["pv_power"].entity_id)]
        if "marstek_auto_mode_button" in missing and out["marstek_mode_select"].entity_id:
            missing.remove("marstek_auto_mode_button")      # a select set by hand does Auto mode too
        if out["battery_soc"].entity_id is None and out["battery_soc_backup"].entity_id is None:
            missing.append("battery_soc")
        return out, missing
    missing = [k for k, f in out.items() if f.entity_id is None and ENTITIES[k][2]
               and not (force and k in SLOT_ONLY)]
    return out, missing


def has_force(services: list) -> bool:
    """Home Assistant has the Solis Modbus force charge, force discharge and stop actions."""
    for dom in services or []:
        if dom.get("domain") == FORCE_DOMAIN:
            return all(s in (dom.get("services") or {}) for s in FORCE_SERVICES)
    return False


def charge_method(opts: Options, services: list) -> tuple[str, str]:
    """`force` or `time_slot` for these settings and actions, and why."""
    found = has_force(services)
    if opts.charge_command == "time_slot":
        return "time_slot", "set in the app settings"
    if opts.charge_command == "force":
        return ("force", "set in the app settings") if found else \
            ("time_slot", "set to force, but Home Assistant has no solis_modbus.solis_force_battery_charge action "
                          "(update the Solis Modbus integration); the time-of-use slots are used")
    return ("force", "found the Solis Modbus force action") if found else \
        ("time_slot", "no Solis Modbus force action found")


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
