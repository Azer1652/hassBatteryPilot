"""The settings form on the page's Settings tab.

Home Assistant's Configuration tab shows every option, whatever the contract or battery. The form
shows the same options in groups and hides what the chosen settings do not use: the formulas of
the other contracts (kept in a closed section, as the comparison still uses them) and the Solis or
Marstek options of the battery you do not have. It saves through the same path as an import
(`settings.prepare`, `App.save_options`), so the Configuration tab stays the one place they live.

Each group or field may carry `show`: {option: [values]}; it is shown only when every named option
has one of those values. A group with `other` is shown, closed, under "Other contracts" when its
`show` does not hold, instead of being hidden.
"""
from __future__ import annotations

import json
import os

from .settings import DEFAULTS, OPTIONAL_NUMBER, OPTIONAL_TEXT, OVERRIDES, RANGES

# The `list(...)` options of config.yaml's schema (tests/test_form.py keeps them the same).
CHOICES = {
    "contract": ["fixed", "dynamic", "variable"],
    "winter_mode": ["full_day", "peaks_only"],
    "battery_system": ["solis", "marstek"],
    "slot_entities": ["time_charging", "grid_time_of_use"],
    "charge_command": ["auto", "force", "time_slot"],
}
# The `match(...)` options: text the Supervisor checks against a pattern.
PATTERNS = {
    "fixed_contract_end": r"^(\d{4}-\d{2}-\d{2})?$",
    "variable_index_month": r"^(\d{4}-(0[1-9]|1[0-2]))?$",
    "night_start": r"^([01][0-9]|2[0-3]):[0-5][0-9]$",
    "night_end": r"^([01][0-9]|2[0-3]):[0-5][0-9]$",
}
SOLIS = {"battery_system": ["solis"]}
MARSTEK = {"battery_system": ["marstek"]}
SOLIS_ENTITIES = ["tou_switch", "grid_charge_switch", "charge_current", "discharge_current",
                  "charge_start", "charge_end", "discharge_start", "discharge_end",
                  "peak_shaving_switch", "peak_shaving_limit", "peak_shaving_soc", "feed_in_priority_switch"]
MARSTEK_ENTITIES = ["marstek_auto_mode_button", "marstek_mode_select", "marstek_last_message",
                    "battery_soc_backup", "battery_charge_power_backup", "battery_discharge_power_backup",
                    "battery_report_time_backup"]
COMMON_ENTITIES = ["grid_power", "grid_import_power", "grid_export_power", "quarter_average_power",
                   "month_peak_power", "house_load_power", "pv_power", "battery_power", "battery_soc",
                   "battery_voltage_sensor", "export_limit", "price_sensor", "forecast_power_now",
                   "forecast_today", "forecast_tomorrow", "car_charger_power"]

GROUPS = [
    {"id": "contract", "title": "Your contract",
     "fields": ["contract", "grid_fees_c_kwh", "injection_fee_c_kwh", "night_start", "night_end",
                "weekend_is_night"]},
    {"id": "fixed", "title": "Fixed contract", "show": {"contract": ["fixed"]}, "other": True,
     "fields": ["fixed_day_rate", "fixed_night_rate", "fixed_sell_mult", "fixed_sell_add",
                "fixed_yearly_fee_eur", "fixed_contract_end"]},
    {"id": "dynamic", "title": "Dynamic contract", "show": {"contract": ["dynamic"]}, "other": True,
     "fields": ["dynamic_buy_mult", "dynamic_buy_add", "dynamic_vat_pct", "dynamic_sell_mult",
                "dynamic_sell_add", "dynamic_yearly_fee_eur"]},
    {"id": "variable", "title": "Variable contract", "show": {"contract": ["variable"]}, "other": True,
     "fields": ["variable_day_mult", "variable_day_add", "variable_night_mult", "variable_night_add",
                "variable_vat_pct", "variable_sell_mult", "variable_sell_add", "variable_yearly_fee_eur",
                "variable_index_eur_mwh", "variable_index_month"]},
    {"id": "grid", "title": "Grid limits and capacity tariff",
     "fields": ["import_limit_kw", "export_limit_kw", "capacity_tariff_eur_kw_year", "capacity_min_kw"]},
    {"id": "battery", "title": "Battery",
     "fields": ["battery_system", "battery_capacity_kwh", "battery_min_soc", "battery_reserve_soc",
                "peak_buffer_pct", "battery_voltage", "max_charge_current_a", "max_discharge_current_a",
                "round_trip_efficiency_pct", "wear_cost_c_kwh", "full_power"]},
    {"id": "solis", "title": "Solis inverter", "show": SOLIS,
     "fields": ["slot_entities", "charge_command", "solis_slave"]},
    {"id": "marstek", "title": "Marstek battery", "show": MARSTEK,
     "fields": ["marstek_device_id", "marstek_stale_s", "marstek_load_includes_battery"]},
    {"id": "winter", "title": "Winter",
     "fields": ["winter_solar_threshold_kwh", "winter_mode", "safety_margin_pct", "winter_hold_soc"]},
    {"id": "car", "title": "Electric car", "fields": ["car_min_kw", "car_battery_kwh", "car_charge_kw"]},
    {"id": "notify", "title": "Notifications", "fields": ["notify_service"]},
    {"id": "entities", "title": "Entities (found automatically: fill in only to choose another one)",
     "closed": True, "fields": COMMON_ENTITIES + SOLIS_ENTITIES + MARSTEK_ENTITIES},
]
# A field shown only for some settings (in a group shown for all).
FIELD_SHOW = ({k: SOLIS for k in SOLIS_ENTITIES} | {k: MARSTEK for k in MARSTEK_ENTITIES}
              | {"safety_margin_pct": {"winter_mode": ["full_day"]}})

TEXT_FILE = os.path.join(os.path.dirname(__file__), "static", "settings_text.json")


def all_fields() -> list[str]:
    return [k for g in GROUPS for k in g["fields"]]


def field(key: str) -> dict:
    """How the page shows one option: its kind, range or choices, and whether it may be empty."""
    if key in CHOICES:
        f = {"kind": "choice", "choices": CHOICES[key]}
    elif key in OVERRIDES:
        f = {"kind": "entity"}
    else:
        want = DEFAULTS[key]
        f = ({"kind": "bool"} if isinstance(want, bool) else {"kind": "int"} if isinstance(want, int)
             else {"kind": "float"} if isinstance(want, float) else {"kind": "text"})
    if key in RANGES:
        f["min"], f["max"] = RANGES[key]
    if key in PATTERNS:
        f["pattern"] = PATTERNS[key]
    f["optional"] = key in OPTIONAL_TEXT or key in OPTIONAL_NUMBER
    if key in FIELD_SHOW:
        f["show"] = FIELD_SHOW[key]
    if key not in OVERRIDES and key not in OPTIONAL_NUMBER:
        f["default"] = DEFAULTS[key]
    return f


def texts() -> dict:
    """{option: {name, description}} from the Configuration tab's text (translations/en.yaml,
    copied into static/settings_text.json by tools/settings_text.py, as the image has no YAML)."""
    try:
        with open(TEXT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def describe(values: dict) -> dict:
    """The form: groups, fields with their kind and text, and the saved values."""
    text = texts()
    fields = {k: {**field(k), **text.get(k, {"name": k})} for k in all_fields()}
    return {"groups": GROUPS, "fields": fields, "values": {k: v for k, v in values.items() if k in fields}}
