"""Energy balance of the house, the battery and the grid for one time step.

`step()` behaves like the inverter does under each command:
* self_use  - the battery covers the house deficit and stores solar surplus;
* charge    - charges from grid (and solar) at the current that keeps grid
              import at the import limit, until the target SOC;
* discharge - discharges at full power (house first, the rest is exported)
              until the target SOC or the reserve.

It is used to replay a plan against measured load and solar (the contract
comparison), to work out the night target, and by the tests.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Options

MIN_CHARGE_DC_KW = 0.5   # about 10 A at 51 V: the controller pauses below this, and holds at it
HOLD_OVER = 2.0          # SOC points a held battery may go above its target within the slot


@dataclass(frozen=True)
class Battery:
    capacity_kwh: float = 16.0
    min_soc: float = 10
    reserve_soc: float = 35
    max_charge_kw: float = 2.56
    max_discharge_kw: float = 4.608
    eta_c: float = 0.9 ** 0.5
    eta_d: float = 0.9 ** 0.5
    wear_c_kwh: float = 2.0
    import_limit_kw: float = 2.3
    export_limit_kw: float = 6.0
    capacity_c_per_kw_month: float = 52.37 / 12 * 100

    @classmethod
    def from_options(cls, o: Options) -> Battery:
        return cls(o.battery_capacity_kwh, o.battery_min_soc, o.battery_reserve_soc, o.max_charge_kw,
                   o.max_discharge_kw, o.charge_efficiency, o.discharge_efficiency, o.wear_cost_c_kwh,
                   o.import_limit_kw, o.export_limit_kw, o.capacity_tariff_eur_kw_year / 12 * 100)

    def kwh(self, soc_points: float) -> float:
        return soc_points * self.capacity_kwh / 100


@dataclass
class StepResult:
    soc: float              # SOC at the end of the step, %
    import_kwh: float
    export_kwh: float
    charge_dc_kwh: float    # into the battery
    discharge_dc_kwh: float  # out of the battery
    curtailed_kwh: float = 0.0


def step(b: Battery, soc: float, action: str, target_soc: float, load_kwh: float, pv_kwh: float,
         hours: float, curtail: bool = False, slot_left_h: float | None = None) -> StepResult:
    """One step of `hours` with this command. `curtail`: export is blocked (sell price < 0).

    `slot_left_h`: time left in the plan slot including this step. Charging is then paced
    to reach the target at the end of the slot, as the controller does, and held at the
    minimum rate once reached (up to HOLD_OVER points above it) so the battery does not
    drain again before the slot ends. Without it, charging goes as fast as allowed.
    """
    net = load_kwh - pv_kwh                      # > 0: house needs energy, < 0: solar surplus
    room = b.kwh(100 - soc)                      # DC kWh the battery can still take
    avail = b.kwh(max(soc - b.min_soc, 0))       # DC kWh above the minimum
    charge_dc = discharge_dc = 0.0

    # Charge power that keeps grid import at the limit, never above the battery maximum.
    follow_kw = min(b.max_charge_kw / b.eta_c, max(b.import_limit_kw - net / hours, 0.0))
    stop = target_soc + (HOLD_OVER if slot_left_h else 0.0)
    charging = action == "charge" and soc < stop and follow_kw * b.eta_c >= MIN_CHARGE_DC_KW
    if charging:
        if slot_left_h:
            want = max(b.kwh(target_soc - soc) * hours / max(slot_left_h, hours), MIN_CHARGE_DC_KW * hours)
        else:
            want = b.kwh(target_soc - soc)
        charge_dc = min(follow_kw * hours * b.eta_c, want, b.kwh(stop - soc), room)
        if net < 0:   # solar surplus goes in anyway, as in self-use
            charge_dc = max(charge_dc, min(-net * b.eta_c, room, b.max_charge_kw * hours))
    elif action == "discharge" and soc > max(target_soc, b.reserve_soc):
        floor = max(target_soc, b.reserve_soc)
        discharge_dc = min(b.max_discharge_kw * hours, b.kwh(soc - floor))
        if net > 0:   # the house deficit also comes out of the battery, down to the minimum
            discharge_dc = max(discharge_dc, min(net / b.eta_d, avail, b.max_discharge_kw * hours))
    else:  # self_use (also charge/discharge that reached their target, or charging paused)
        if net > 0:
            discharge_dc = min(net / b.eta_d, avail, b.max_discharge_kw * hours)
        else:
            charge_dc = min(-net * b.eta_c, room, b.max_charge_kw * hours)

    grid = net + charge_dc / b.eta_c - discharge_dc * b.eta_d
    imp, exp = max(grid, 0.0), max(-grid, 0.0)
    curtailed = 0.0
    if exp > b.export_limit_kw * hours:
        curtailed, exp = exp - b.export_limit_kw * hours, b.export_limit_kw * hours
    if curtail and exp > 0:
        curtailed, exp = curtailed + exp, 0.0
    new_soc = soc + (charge_dc - discharge_dc) * 100 / b.capacity_kwh
    return StepResult(min(max(new_soc, 0.0), 100.0), imp, exp, charge_dc, discharge_dc, curtailed)
