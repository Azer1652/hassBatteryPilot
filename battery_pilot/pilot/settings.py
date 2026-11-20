"""Export and import of the app's settings (the options on the Configuration tab).

Pure functions: `export_doc` builds the file, `prepare` turns an imported file into the
options to save and the list of what changes. Saving goes through the Supervisor (`App.save_options`).
"""
from __future__ import annotations

from dataclasses import fields
from datetime import datetime

from .config import OVERRIDE_OPTION, ConfigError, Options, from_dict

APP = "battery_pilot"
OVERRIDES = frozenset(OVERRIDE_OPTION.values())       # entity names: optional, left out when empty
DEFAULTS = {f.name: getattr(Options(), f.name) for f in fields(Options) if f.name != "entities"}
KNOWN = frozenset(DEFAULTS) | OVERRIDES
OPTIONAL_TEXT = OVERRIDES | {"notify_service", "marstek_device_id"}        # `str?` in config.yaml: left out when empty
# The ranges of config.yaml's schema (that file is not in the image). The Supervisor refuses a save
# outside them, so an import is checked here first. tests/test_settings.py keeps the two the same.
RANGES = {
    "fixed_day_rate": (0, 100), "fixed_night_rate": (0, 100), "fixed_yearly_fee_eur": (0, 1000),
    "fixed_sell_mult": (0, 1), "fixed_sell_add": (-50, 50),
    "dynamic_buy_mult": (0, 1), "dynamic_buy_add": (-20, 50), "dynamic_vat_pct": (0, 30),
    "dynamic_sell_mult": (0, 1), "dynamic_sell_add": (-50, 50), "dynamic_yearly_fee_eur": (0, 1000),
    "variable_day_mult": (0, 1), "variable_day_add": (-20, 50), "variable_night_mult": (0, 1),
    "variable_night_add": (-20, 50), "variable_vat_pct": (0, 30), "variable_sell_mult": (0, 1),
    "variable_sell_add": (-50, 50), "variable_yearly_fee_eur": (0, 1000), "variable_index_eur_mwh": (0, 1000),
    "grid_fees_c_kwh": (0, 60), "injection_fee_c_kwh": (0, 20),
    "import_limit_kw": (0.5, 50), "export_limit_kw": (0, 50), "inverter_ac_kw": (0, 50),
    "capacity_tariff_eur_kw_year": (0, 500), "capacity_min_kw": (0, 20),
    "car_min_kw": (0, 25), "car_battery_kwh": (0, 200), "car_charge_kw": (0, 25),
    "battery_capacity_kwh": (1, 200), "battery_min_soc": (0, 50), "battery_reserve_soc": (0, 95),
    "peak_buffer_pct": (0, 50), "battery_voltage": (12, 1000),
    "max_charge_current_a": (5, 500), "max_discharge_current_a": (5, 500),
    "round_trip_efficiency_pct": (50, 100), "wear_cost_c_kwh": (0, 50), "safety_margin_pct": (0, 60),
    "winter_solar_threshold_kwh": (0, 200), "winter_hold_soc": (0, 100), "solis_slave": (1, 247),
    "marstek_stale_s": (30, 3600),
}


def export_doc(options: dict, version: str, now: datetime) -> dict:
    return {"app": APP, "version": version, "exported": now.isoformat(timespec="seconds"),
            "options": {k: v for k, v in options.items() if k in KNOWN}}


def _typed(key: str, value):
    """The value in the type the setting has (the Supervisor checks types strictly)."""
    want = "" if key in OVERRIDES else DEFAULTS[key]
    number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(want, bool):
        if isinstance(value, bool):
            return value
        raise ConfigError(f"{key} must be true or false, not {value!r}.")
    if isinstance(want, int):
        if number and float(value).is_integer():
            return _in_range(key, int(value))
        raise ConfigError(f"{key} must be a whole number, not {value!r}.")
    if isinstance(want, float):
        if number:
            return _in_range(key, value)
        raise ConfigError(f"{key} must be a number, not {value!r}.")
    if isinstance(value, str):
        return value.strip() if key in OPTIONAL_TEXT else value
    raise ConfigError(f"{key} must be text, not {value!r}.")


def _in_range(key: str, value):
    lo, hi = RANGES.get(key, (None, None))
    if lo is not None and not lo <= value <= hi:
        raise ConfigError(f"{key} must be between {lo} and {hi}, not {value!r}.")
    return value


def prepare(raw, current: dict) -> tuple[dict, list[dict], list[str]]:
    """(options to save, changes [{key, old, new}], ignored keys) for an imported file.

    `raw` is an export file or a bare options object. A setting the file does not have keeps its
    current value, so a file from an older version still works. Raises ConfigError when the file
    or a value in it is not usable; nothing is saved then.
    """
    if isinstance(raw, dict) and isinstance(raw.get("options"), dict):
        raw = raw["options"]
    if not isinstance(raw, dict):
        raise ConfigError("this is not a Battery Pilot settings file.")
    ignored = sorted(str(k) for k in raw if k not in KNOWN)
    new = {k: _typed(k, v) for k, v in raw.items() if k in KNOWN and v is not None}
    if not new:
        raise ConfigError("this file holds no Battery Pilot settings.")
    merged = {**current, **new}
    merged = {k: v for k, v in merged.items() if not (k in OPTIONAL_TEXT and not str(v or "").strip())}
    try:
        from_dict(merged)
    except ConfigError:
        raise
    except Exception as e:  # noqa: BLE001 - a value of the wrong kind in the saved settings
        raise ConfigError(f"these settings cannot be used ({e}).") from None

    def shown(d, k):
        v = d.get(k)
        return None if k in OPTIONAL_TEXT and not str(v or "").strip() else v

    changes = [{"key": k, "old": shown(current, k), "new": shown(merged, k)}
               for k in sorted(set(current) | set(merged)) if shown(current, k) != shown(merged, k)]
    return merged, changes, ignored
