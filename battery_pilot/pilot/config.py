"""App options: typed, with defaults that match the original Flemish setup.

Home Assistant validates types and ranges against `config.yaml` before the app
starts; this module adds the checks that the schema cannot express and turns
the options into one object the rest of the app reads.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields
from datetime import date, time

# Entity override keys (empty = find automatically). Order is the log order.
ENTITY_KEYS = (
    "grid_import_power", "grid_export_power", "house_load_power", "pv_power",
    "battery_soc", "battery_voltage", "tou_switch", "grid_charge_switch",
    "charge_current", "discharge_current", "charge_start", "charge_end",
    "discharge_start", "discharge_end", "export_limit", "peak_shaving_switch", "peak_shaving_limit",
    "peak_shaving_soc", "feed_in_priority_switch", "forecast_power_now",
    "forecast_today", "forecast_tomorrow", "price_sensor", "car_charger_power",
)
# Option name of each override where it differs from the entity key
# (battery_voltage is already the nominal-voltage option).
OVERRIDE_OPTION = {k: k for k in ENTITY_KEYS} | {"battery_voltage": "battery_voltage_sensor"}

_HHMM = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


class ConfigError(ValueError):
    """An option combination the schema allows but the app cannot use."""


@dataclass
class Options:
    # contract
    contract: str = "fixed"                  # the contract you are on now: fixed | dynamic
    fixed_contract_end: str = ""             # YYYY-MM-DD, for renewal reminders
    grid_fees_c_kwh: float = 11.96           # added to every bought kWh, both contracts, incl. VAT
    injection_fee_c_kwh: float = 0.19        # subtracted from every sold kWh, both contracts
    fixed_day_rate: float = 18.59            # c/kWh incl. VAT, without grid fees
    fixed_night_rate: float = 15.22
    fixed_sell_mult: float = 0.0798          # fixed sell = mult x month avg market (EUR/MWh) + add
    fixed_sell_add: float = -2.72
    fixed_yearly_fee_eur: float = 65.0
    dynamic_buy_mult: float = 0.102          # dynamic buy = (mult x market + add) x (1 + VAT) + grid fees
    dynamic_buy_add: float = 1.0
    dynamic_vat_pct: float = 6.0
    dynamic_sell_mult: float = 0.1           # dynamic sell = mult x market + add - injection fee
    dynamic_sell_add: float = -1.188
    dynamic_yearly_fee_eur: float = 100.0
    night_start: str = "22:00"               # night tariff (fixed contract) and night charging window
    night_end: str = "07:00"
    weekend_is_night: bool = True
    # grid
    import_limit_kw: float = 2.3
    export_limit_kw: float = 5.0
    capacity_tariff_eur_kw_year: float = 52.37
    capacity_min_kw: float = 2.5
    # battery
    battery_capacity_kwh: float = 16.0
    battery_min_soc: int = 10
    battery_reserve_soc: int = 35
    battery_voltage: float = 51.2
    max_charge_current_a: int = 50
    max_discharge_current_a: int = 90
    round_trip_efficiency_pct: int = 90
    wear_cost_c_kwh: float = 2.0
    # planning
    safety_margin_pct: int = 30
    winter_solar_threshold_kwh: float = 12.0
    winter_mode: str = "full_day"            # full_day: fill at night to carry the day | peaks_only
    # electric car (0 = no car detection)
    car_min_kw: float = 0.0                  # a jump in house load of at least this much, that stays, is the car
    # inverter
    slot_entities: str = "time_charging"
    notify_service: str = ""
    entities: dict = field(default_factory=dict)

    # ---- derived values ----
    @property
    def charge_efficiency(self) -> float:
        return (self.round_trip_efficiency_pct / 100) ** 0.5

    @property
    def discharge_efficiency(self) -> float:
        return (self.round_trip_efficiency_pct / 100) ** 0.5

    @property
    def max_charge_kw(self) -> float:
        return self.max_charge_current_a * self.battery_voltage / 1000

    @property
    def max_discharge_kw(self) -> float:
        return self.max_discharge_current_a * self.battery_voltage / 1000

    @property
    def night_start_time(self) -> time:
        return _parse_hhmm(self.night_start)

    @property
    def night_end_time(self) -> time:
        return _parse_hhmm(self.night_end)

    @property
    def fixed_end_date(self) -> date | None:
        s = (self.fixed_contract_end or "").strip()
        return date.fromisoformat(s) if s else None

    def validate(self) -> None:
        if self.contract not in ("fixed", "dynamic"):
            raise ConfigError(f"contract must be 'fixed' or 'dynamic', not {self.contract!r}.")
        for name in ("night_start", "night_end"):
            if not _HHMM.match(getattr(self, name)):
                raise ConfigError(f"{name} must look like 22:00, not {getattr(self, name)!r}.")
        if self.night_start == self.night_end:
            raise ConfigError("night_start and night_end are the same: the night window would be empty.")
        if not self.battery_min_soc <= self.battery_reserve_soc <= 100:
            raise ConfigError("battery_reserve_soc must be between battery_min_soc and 100.")
        if self.winter_mode not in ("full_day", "peaks_only"):
            raise ConfigError("winter_mode must be full_day or peaks_only.")
        if self.slot_entities not in ("time_charging", "grid_time_of_use"):
            raise ConfigError("slot_entities must be time_charging or grid_time_of_use.")
        if self.fixed_contract_end.strip():
            try:
                _ = self.fixed_end_date
            except ValueError:
                raise ConfigError(f"fixed_contract_end must look like 2027-03-31, not {self.fixed_contract_end!r}.") from None


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def from_dict(raw: dict) -> Options:
    """Build Options from the add-on's options.json (unknown keys are ignored)."""
    known = {f.name for f in fields(Options)} - {"entities"}
    kwargs = {k: v for k, v in raw.items() if k in known and v is not None}
    for k in ("fixed_contract_end", "notify_service"):
        if k in kwargs:
            kwargs[k] = str(kwargs[k]).strip()
    opts = Options(**kwargs)
    opts.entities = {k: str(raw[o]).strip() for k, o in OVERRIDE_OPTION.items() if str(raw.get(o) or "").strip()}
    opts.validate()
    return opts


def load(path: str) -> Options:
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError(f"the settings file {path} cannot be read ({e}).") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"the settings file {path} does not hold settings.")
    return from_dict(raw)
