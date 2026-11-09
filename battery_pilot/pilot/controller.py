"""From the plan's current step and live readings to one inverter command.

Pure function, so every rule can be tested without Home Assistant:

* charge    - grid-charge towards the step's SOC, spread over the rest of the
              slot, never above the current that keeps grid import at the
              import limit (house load is followed live); pause when the
              quarter-hour heads over the limit (the old playbook's rule);
* discharge - discharge towards the step's SOC, never below the reserve;
* self_use  - also when a target is reached: never charge above it (the old
              playbook's overcharge bug).

Charge and discharge slots are renewed at each new clock hour.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import Options
from .planner import Step

AC_DC = 1.03        # inverter losses AC -> DC while charging (as in the old playbook)
STEP_A = 5          # Solis current steps
MIN_A = 10          # below this, charging is paused (old playbook)
START_A = 15        # start charging only with some headroom (old playbook)


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
    command: str             # none | charge | discharge | current | self_use
    amps: int = 0
    reason: str = ""


def follow_amps(r: Readings, o: Options) -> int:
    """Charge current that keeps grid import at the limit with the current house load."""
    v = r.voltage if r.voltage > 40 else o.battery_voltage
    a = (o.import_limit_kw - r.load_kw + r.pv_kw) * 1000 / v / AC_DC
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
           control_on: bool) -> Decision:
    hour = r.t_local.strftime("%H:00:00")
    if not control_on:
        if mode in ("charge", "discharge"):
            return Decision("self_use", reason="Battery Pilot control is off")
        return Decision("none", reason="shadow mode: control is off")
    if step is None:
        if mode != "self_use":
            return Decision("self_use", reason="no plan")
        return Decision("none", reason="no plan")

    if step.action == "charge" and r.soc < step.soc_end:
        follow = follow_amps(r, o)
        want = min(follow, paced_amps(step, r, o, up=True))
        if mode == "charge":
            if r.projection_kw > o.import_limit_kw + 0.1 or follow < MIN_A:
                return Decision("self_use", reason="house load too high: battery helps instead")
            if slot_hour != hour:
                return Decision("charge", want, "renew the hourly charge slot")
            if abs(want - amps_now) >= 10:
                return Decision("current", want, "follow house load")
            return Decision("none", reason="charging")
        if follow >= START_A and r.projection_kw < o.import_limit_kw - 0.2:
            return Decision("charge", want, f"plan: charge to {step.soc_end:.0f} %")
        if mode != "self_use":
            return Decision("self_use", reason="waiting for room under the import limit")
        return Decision("none", reason="waiting for room under the import limit")

    floor = max(step.soc_end, o.battery_reserve_soc)
    if step.action == "discharge" and r.soc > floor:
        want = paced_amps(step, r, o, up=False)
        if mode != "discharge" or slot_hour != hour:
            return Decision("discharge", want, f"plan: export down to {floor:.0f} %")
        if abs(want - amps_now) >= 10:
            return Decision("current", want, "pace the export")
        return Decision("none", reason="discharging")

    if mode != "self_use":
        why = "target reached" if step.action != "self_use" else "plan: self-use"
        return Decision("self_use", reason=why)
    return Decision("none", reason="self-use")
