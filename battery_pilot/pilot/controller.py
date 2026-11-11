"""From the plan's current step and live readings to one inverter command.

Pure function, so every rule can be tested without Home Assistant:

* charge    - grid-charge towards the step's SOC, spread over the rest of the
              slot, never above the current that keeps grid import at the
              import limit, or at the step's higher limit when the plan pays for
              a higher peak (house load is followed live); pause when the
              quarter-hour heads over the limit (the old playbook's rule);
              once the SOC is reached, hold at the minimum current until the
              slot ends (at most HOLD_OVER points above the target);
* discharge - discharge towards the step's SOC, never below the reserve (or the
              step's own floor, when the plan sells below it at an extreme price);
* peak_shave - the inverter's own peak-shaving mode at the import limit: the
              grid feeds the house, the battery only what is above the limit;
* solar_hold - the inverter's feed-in priority while there is solar surplus: it is
              exported, the battery waits; without surplus back to self-use (the
              battery may not feed the house in that mode);
* self_use  - also when a charge target is passed: never charge on above it
              (the old playbook's overcharge bug).

Charge and discharge slots are renewed at each new clock hour.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import Options
from .planner import Step
from .simulate import HOLD_OVER

AC_DC = 1.03        # inverter losses AC -> DC while charging (as in the old playbook)
STEP_A = 5          # Solis current steps
MIN_A = 10          # below this, charging is paused (old playbook)
START_A = 15        # start charging only with some headroom (old playbook)


# Modes that are ours to end: control off, a stop or lost measurements return the inverter to
# self-use (failed: a command of ours may have half-applied).
ACTIVE = ("charge", "discharge", "peak_shave", "solar_hold", "failed")
HOLD_SURPLUS_KW = 0.3   # solar hold starts with this much surplus
HOLD_DWELL_S = 120      # and is not started or ended within this time of the last command


@dataclass
class Readings:
    t: datetime              # UTC
    t_local: datetime
    soc: float
    load_kw: float
    pv_kw: float
    import_kw: float
    projection_kw: float     # where this quarter-hour's average import is heading
    voltage: float


@dataclass
class Decision:
    command: str             # none | charge | discharge | current | self_use | peak_shave | solar_hold
    amps: int = 0
    reason: str = ""


def follow_amps(r: Readings, o: Options, limit_kw: float | None = None) -> int:
    """Charge current that keeps grid import at the limit with the current house load."""
    v = r.voltage if r.voltage > 40 else o.battery_voltage
    limit = o.import_limit_kw if limit_kw is None else limit_kw
    a = (limit - r.load_kw + r.pv_kw) * 1000 / v / AC_DC
    return int(max(min((a // STEP_A) * STEP_A, o.max_charge_current_a), 0))


def paced_amps(step: Step, r: Readings, o: Options, up: bool) -> int:
    """Current that reaches the step's SOC by the end of its slot."""
    v = r.voltage if r.voltage > 40 else o.battery_voltage
    end = step.start + timedelta(hours=step.hours)
    hours = max((end - r.t).total_seconds() / 3600, 5 / 60)
    kwh = abs(step.soc_end - r.soc) / 100 * o.battery_capacity_kwh
    a = kwh * 1000 / hours / v
    a = int(math.ceil(a / STEP_A) * STEP_A)
    limit = o.max_charge_current_a if up else o.max_discharge_current_a
    return max(min(a, limit), MIN_A)


def decide(step: Step | None, r: Readings, o: Options, mode: str, slot_hour: str, amps_now: int,
           control_on: bool, mode_age_s: float = 1e9) -> Decision:
    hour = r.t_local.strftime("%H:00:00")
    if not control_on:
        if mode in ACTIVE:
            return Decision("self_use", reason="Battery Pilot control is off")
        return Decision("none", reason="shadow mode: control is off")
    if step is None:
        if mode != "self_use":
            return Decision("self_use", reason="no plan")
        return Decision("none", reason="no plan")

    # The plan may charge above the import limit (when that pays) or sell below the reserve
    # (at an extreme price); the step says so.
    limit = step.limit_kw if step.limit_kw is not None else o.import_limit_kw
    if step.action == "charge" and r.soc < step.soc_end + HOLD_OVER:
        follow = follow_amps(r, o, limit)
        # Paced to reach the target at the end of the slot; once there, held at the minimum
        # current so the battery does not drain again before the slot ends.
        paced = paced_amps(step, r, o, up=True) if r.soc < step.soc_end else MIN_A
        want = min(follow, paced)
        if mode == "failed" and follow >= MIN_A:
            return Decision("charge", want, "send the charge command again")
        if mode == "charge":
            if follow < MIN_A:
                return Decision("self_use", reason="house load too high: battery helps instead")
            if r.projection_kw > limit + 0.1:
                if want < amps_now:       # a lower current keeps the quarter-hour under the limit
                    return Decision("current", want, "house load up: lower the charge current")
                return Decision("self_use", reason="house load too high: battery helps instead")
            if slot_hour != hour:
                return Decision("charge", want, "renew the hourly charge slot")
            if abs(want - amps_now) >= 10:
                return Decision("current", want, "follow house load")
            return Decision("none", reason="charging")
        if follow >= START_A and r.projection_kw < limit - 0.2:
            return Decision("charge", want, f"plan: charge to {step.soc_end:.0f} %")
        if mode != "self_use":
            return Decision("self_use", reason="waiting for room under the import limit")
        return Decision("none", reason="waiting for room under the import limit")

    floor = max(step.soc_end, step.reserve if step.reserve is not None else o.battery_reserve_soc)
    if step.action == "discharge" and r.soc > floor:
        want = paced_amps(step, r, o, up=False)
        if mode != "discharge" or slot_hour != hour:
            return Decision("discharge", want, f"plan: export down to {floor:.0f} %")
        if abs(want - amps_now) >= 10:
            return Decision("current", want, "pace the export")
        return Decision("none", reason="discharging")

    if step.action == "peak_shave":
        if mode != "peak_shave":
            return Decision("peak_shave", reason="plan: peak shaving (the battery is kept for later)")
        return Decision("none", reason="peak shaving")

    if step.action == "solar_hold":
        surplus = r.pv_kw - r.load_kw
        if mode == "solar_hold":
            if surplus >= 0 or mode_age_s < HOLD_DWELL_S:
                return Decision("none", reason="solar hold: surplus goes to the grid")
            return Decision("self_use", reason="no solar surplus: the battery feeds the house")
        if surplus >= HOLD_SURPLUS_KW and (mode != "self_use" or mode_age_s >= HOLD_DWELL_S):
            return Decision("solar_hold", reason="plan: solar surplus to the grid (room kept for later)")
        if mode != "self_use":
            return Decision("self_use", reason="solar hold waits for surplus")
        return Decision("none", reason="solar hold waits for surplus")

    if mode != "self_use":
        why = "target reached" if step.action != "self_use" else "plan: self-use"
        return Decision("self_use", reason=why)
    return Decision("none", reason="self-use")
