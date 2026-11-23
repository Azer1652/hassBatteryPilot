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
    "grid_import_power", "grid_export_power", "house_load_power", "pv_power", "battery_power", "inverter_grid_power",
    "battery_soc", "battery_voltage", "tou_switch", "grid_charge_switch",
    "charge_current", "discharge_current", "charge_start", "charge_end",
    "discharge_start", "discharge_end", "export_limit", "peak_shaving_switch", "peak_shaving_limit",
    "peak_shaving_soc", "feed_in_priority_switch", "self_use_switch", "forecast_power_now",
    "forecast_today", "forecast_tomorrow", "price_sensor", "car_charger_power",
    "grid_power", "quarter_average_power", "month_peak_power", "marstek_auto_mode_button", "marstek_mode_select",
    "marstek_last_message",
    "battery_soc_backup",
    "battery_charge_power_backup", "battery_discharge_power_backup", "battery_report_time_backup",
)
# Option name of each override where it differs from the entity key
# (battery_voltage is already the nominal-voltage option).
OVERRIDE_OPTION = {k: k for k in ENTITY_KEYS} | {"battery_voltage": "battery_voltage_sensor"}

_YYYYMM = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_HHMM = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


class ConfigError(ValueError):
    """An option combination the schema allows but the app cannot use."""


@dataclass
class Options:
    # contract
    contract: str = "fixed"                  # the contract you are on now: fixed | dynamic | variable
    fixed_contract_end: str = ""             # YYYY-MM-DD, for renewal reminders
    grid_fees_c_kwh: float = 11.33           # added to every bought kWh, both contracts, incl. VAT
    injection_fee_c_kwh: float = 0.19        # subtracted from every sold kWh, both contracts
    fixed_day_rate: float = 18.59            # c/kWh incl. VAT, without grid fees
    fixed_night_rate: float = 15.22
    fixed_sell_mult: float = 0.0798          # fixed sell = mult x month avg market (EUR/MWh) + add
    fixed_sell_add: float = -2.72
    fixed_yearly_fee_eur: float = 65.0
    dynamic_buy_mult: float = 0.102          # dynamic buy = (mult x market + add) x (1 + VAT) + grid fees
    dynamic_buy_add: float = 0.408
    dynamic_vat_pct: float = 6.0
    dynamic_sell_mult: float = 0.1           # dynamic sell = mult x market + add - injection fee
    dynamic_sell_add: float = -1.3
    dynamic_yearly_fee_eur: float = 15.0
    # variable (monthly index): buy = (mult x index + add) x (1 + VAT) + grid fees, day and night apart
    # (night 0 and 0: the day formula); sell = sell mult x index + sell add - injection fee.
    # Index: the month's average market price, or the published one for the month it is set for.
    # Day factor 0: no variable contract (it is left out of the comparison).
    variable_day_mult: float = 0.0
    variable_day_add: float = 0.0
    variable_night_mult: float = 0.0
    variable_night_add: float = 0.0
    variable_vat_pct: float = 6.0
    variable_sell_mult: float = 0.0
    variable_sell_add: float = 0.0
    variable_yearly_fee_eur: float = 0.0
    variable_index_eur_mwh: float = 0.0      # a month's published index (0: none; the market average is used)
    variable_index_month: str = ""           # YYYY-MM: the month that index is for
    night_start: str = "22:00"               # night tariff (fixed contract) and night charging window
    night_end: str = "07:00"
    weekend_is_night: bool = True
    # grid
    import_limit_kw: float = 2.3
    export_limit_kw: float = 5.0
    inverter_ac_kw: float = 0.0              # the hybrid inverter's rated AC output (0: not limited)
    capacity_tariff_eur_kw_year: float = 52.37
    capacity_min_kw: float = 2.5
    # battery
    battery_capacity_kwh: float = 16.0
    battery_min_soc: int = 10
    battery_reserve_soc: int = 35
    peak_buffer_pct: int = 0          # SOC points above the minimum kept for peaks (0: off)
    battery_voltage: float = 51.2
    max_charge_current_a: int = 50
    max_discharge_current_a: int = 90
    round_trip_efficiency_pct: int = 90
    wear_cost_c_kwh: float = 2.0
    # planning
    safety_margin_pct: int = 30
    winter_solar_threshold_kwh: float = 12.0
    winter_mode: str = "full_day"            # full_day: fill at night to carry the day | peaks_only
    winter_hold_soc: int = 0                 # winter days: self-use stops here, below it peak shaving (0: off)
    # electric car (0 = no car detection)
    car_min_kw: float = 0.0                  # a jump in house load of at least this much, that stays, is the car
    car_battery_kwh: float = 70.5            # the car's battery for the page's coverage estimate (0: none); a Mercedes EQB 250+
    car_charge_kw: float = 5.0               # the car's charging power (0: only the measured power while it charges)
    # inverter
    battery_system: str = "solis"            # solis: a Solis hybrid | marstek: a Marstek Venus (local API, cloud backup)
    marstek_device_id: str = ""              # the Marstek battery's HA device (empty: found from its entities)
    marstek_stale_s: int = 300               # a Marstek reading older than this is not used (the other source is)
    marstek_load_includes_battery: bool = True   # the house-load sensor counts the Marstek's charging as load
    full_power: bool = False                 # charge and discharge as fast as the limits allow, not paced
    slot_entities: str = "time_charging"
    charge_command: str = "auto"             # auto: the force action when HA has it | force | time_slot
    solis_slave: int = 0                     # Modbus slave ID for the force action (0: the integration's default)
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
    def marstek(self) -> bool:
        return self.battery_system == "marstek"

    @property
    def ac_max_kw(self) -> float:
        """The inverter's AC output limit for the plan (0: none). A Marstek beside another
        inverter is AC-coupled: it cannot take what that inverter holds back."""
        return 0.0 if self.marstek else self.inverter_ac_kw

    @property
    def variable_set(self) -> bool:
        """A variable contract is filled in (or is the contract now): it is priced and compared too."""
        return self.contract == "variable" or self.variable_day_mult > 0

    @property
    def variable_night(self) -> tuple[float, float]:
        """(factor, fixed part) of the night formula; the day one when the night one is not filled in
        (a single-rate meter), so a night is never priced at the grid fees alone."""
        if self.variable_night_mult <= 0 and self.variable_night_add == 0:
            return self.variable_day_mult, self.variable_day_add
        return self.variable_night_mult, self.variable_night_add

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
        if self.contract not in ("fixed", "dynamic", "variable"):
            raise ConfigError(f"contract must be 'fixed', 'dynamic' or 'variable', not {self.contract!r}.")
        if self.contract == "variable" and self.variable_day_mult <= 0:
            raise ConfigError("contract is variable but its formula is not filled in: set variable_day_mult "
                              "(the factor before the index on your supplier's tariff sheet), and the rest of it.")
        if self.variable_index_month.strip() and not _YYYYMM.match(self.variable_index_month.strip()):
            raise ConfigError(f"variable_index_month must look like 2026-11, not {self.variable_index_month!r}.")
        if self.variable_index_eur_mwh > 0 and not self.variable_index_month.strip():
            raise ConfigError("variable_index_eur_mwh is set but variable_index_month is empty: say which month "
                              "(YYYY-MM) the index is for.")
        for name in ("night_start", "night_end"):
            if not _HHMM.match(getattr(self, name)):
                raise ConfigError(f"{name} must look like 22:00, not {getattr(self, name)!r}.")
        if self.night_start == self.night_end:
            raise ConfigError("night_start and night_end are the same: the night window would be empty.")
        if not self.battery_min_soc <= self.battery_reserve_soc <= 100:
            raise ConfigError("battery_reserve_soc must be between battery_min_soc and 100.")
        if not 0 <= self.peak_buffer_pct or self.battery_min_soc + self.peak_buffer_pct > 90:
            raise ConfigError("peak_buffer_pct: the battery minimum plus the peak buffer must stay at or below 90 %.")
        if self.winter_hold_soc and not self.battery_min_soc <= self.winter_hold_soc <= 100:
            raise ConfigError("winter_hold_soc must be 0 (off) or between battery_min_soc and 100.")
        if self.winter_mode not in ("full_day", "peaks_only"):
            raise ConfigError("winter_mode must be full_day or peaks_only.")
        if self.slot_entities not in ("time_charging", "grid_time_of_use"):
            raise ConfigError("slot_entities must be time_charging or grid_time_of_use.")
        if self.battery_system not in ("solis", "marstek"):
            raise ConfigError("battery_system must be solis or marstek.")
        if self.charge_command not in ("auto", "force", "time_slot"):
            raise ConfigError("charge_command must be auto, force or time_slot.")
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
    for k in ("fixed_contract_end", "notify_service", "marstek_device_id", "variable_index_month"):
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
