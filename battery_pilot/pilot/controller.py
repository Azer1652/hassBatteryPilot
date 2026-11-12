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
* the car   - while the electric car charges and the house asks more than the import
              limit, peak shaving whatever the step says (when the inverter has it);
* the fill switch - while it is on and the car does not charge: grid charge to 100 % under
              the import limit, then keep the battery (peak shaving when the inverter has it,
              else the hold at the minimum current); it charges again below FILL_AGAIN;
* the peak buffer - with `peak_buffer_pct` and the peak-shaving switch: where the plan says
              self-use and the SOC is at or below minimum + buffer, peak shaving; BUFFER_GAP
              below it, grid charge back to it under the import limit;
* the winter level - a plan step with `hold` (a winter day, `winter_hold_soc`): at or below it
              peak shaving, nothing charged back from the grid; self-use again only WINTER_BAND
              above it, and no switch within WINTER_DWELL_S of the last, so the inverter does not
              flip between the two;
* self_use  - also when a charge target is passed: never charge on above it
              (the old playbook's overcharge bug).

Charge and discharge slots are renewed at each new clock hour, and a force command (the Solis
Modbus action) also every FORCE_RENEW_S, before its timeout ends it.
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
BUFFER_GAP = 3.0        # the peak buffer is charged again when it is this far below its level
BUFFER_BAND = 2.0       # and peak shaving at the buffer ends this far above it
WINTER_BAND = 5.0       # the winter level: peak shaving from the level, self-use again only this far above it
WINTER_DWELL_S = 900    # and no switch between the two within this time of the last mode change
FILL_AGAIN = 95.0       # the fill switch: a full battery is charged again below this SOC


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


def fill_step(r: Readings, mode: str, can_shave: bool) -> Step:
    """The step the fill switch puts in the plan's place: charge to 100 % within this clock hour
    (so at the current the import limit allows), and once full keep the battery."""
    start = r.t.replace(minute=0, second=0, microsecond=0)
    filling = r.soc < FILL_AGAIN or (mode in ("charge", "failed") and r.soc < 100)
    action = "charge" if filling or not can_shave else "peak_shave"
    return Step(start, 1.0, action, r.soc, 100.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def decide(step: Step | None, r: Readings, o: Options, mode: str, slot_hour: str, amps_now: int,
           control_on: bool, mode_age_s: float = 1e9, car_kw: float = 0.0, can_shave: bool = False,
           fill: bool = False, renew: bool = False) -> Decision:
    """`car_kw`: what the electric car takes now (0: not charging). `can_shave`: the inverter has
    the peak-shaving switch. `fill`: the "fill the battery for the car" switch is on. `renew`: the
    running force charge or discharge must be sent again before it lapses (`Solis.renew_due`)."""
    hour = r.t_local.strftime("%H:00:00")
    renewal = ("renew the hourly charge slot" if slot_hour != hour
               else "renew the force command" if renew else "")
    if not control_on:
        if mode in ACTIVE:
            return Decision("self_use", reason="Battery Pilot control is off")
        return Decision("none", reason="shadow mode: control is off")
    # The car charges and the house asks more than the import limit: the grid gives up to the limit
    # and the battery only the rest, whatever the plan says. In self-use the battery would feed the
    # whole car, run empty and leave the full charge to the grid (a capacity peak). It stays so until
    # the car stops, also through a moment of sun.
    if car_kw > 0 and can_shave and (mode == "peak_shave" or r.load_kw - r.pv_kw > o.import_limit_kw):
        if mode != "peak_shave":
            return Decision("peak_shave", reason="the car is charging: the grid gives up to the import limit, "
                                                 "the battery the rest")
        return Decision("none", reason="peak shaving while the car charges")
    src = "plan"
    if fill and car_kw <= 0:
        step, src = fill_step(r, mode, can_shave), "fill switch"
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
            if renewal:
                return Decision("charge", want, renewal)
            if abs(want - amps_now) >= 10:
                return Decision("current", want, "follow house load")
            return Decision("none", reason="charging")
        if follow >= START_A and r.projection_kw < limit - 0.2:
            return Decision("charge", want, f"{src}: charge to {step.soc_end:.0f} %")
        if mode != "self_use":
            return Decision("self_use", reason="waiting for room under the import limit")
        return Decision("none", reason="waiting for room under the import limit")

    floor = max(step.soc_end, step.reserve if step.reserve is not None else o.battery_reserve_soc)
    if step.action == "discharge" and r.soc > floor:
        want = paced_amps(step, r, o, up=False)
        if mode != "discharge" or renewal:
            return Decision("discharge", want, f"plan: export down to {floor:.0f} %")
        if abs(want - amps_now) >= 10:
            return Decision("current", want, "pace the export")
        return Decision("none", reason="discharging")

    # The peak buffer: at or below it the battery is kept for what the house asks above the import
    # limit (peak shaving); used by a peak, it is charged again under the import limit.
    if can_shave and o.peak_buffer_pct > 0 and not (step.action == "solar_hold" and r.pv_kw >= r.load_kw):
        level = o.battery_min_soc + o.peak_buffer_pct
        if r.soc <= level - BUFFER_GAP or (mode in ("charge", "failed") and r.soc < level):
            refill = Step(r.t.replace(minute=0, second=0, microsecond=0), 1.0, "charge", r.soc, float(level),
                          0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
            follow = follow_amps(r, o)
            want = min(follow, paced_amps(refill, r, o, up=True))
            if mode == "charge":
                if follow < MIN_A or r.projection_kw > o.import_limit_kw + 0.1:
                    return Decision("peak_shave", reason="peak buffer: the house needs the room under the limit")
                if renewal:
                    return Decision("charge", want, renewal)
                if abs(want - amps_now) >= 10:
                    return Decision("current", want, "follow house load")
                return Decision("none", reason="charging the peak buffer")
            if follow >= START_A and r.projection_kw < o.import_limit_kw - 0.2:
                return Decision("charge", want, f"peak buffer: charge back to {level} %")
        if r.soc <= level or (mode == "peak_shave" and r.soc < level + BUFFER_BAND):
            if mode != "peak_shave":
                return Decision("peak_shave", reason=f"peak buffer: below {level} % the battery only covers "
                                                     "what is above the import limit")
            return Decision("none", reason="peak buffer kept (peak shaving)")

    if can_shave and step.hold > 0 and step.action in ("self_use", "peak_shave") and r.soc < step.hold + WINTER_BAND:
        held = winter_hold(step.hold, r, mode, mode_age_s)
        if held is not None:
            return held

    if step.action == "peak_shave":
        if mode != "peak_shave":
            return Decision("peak_shave", reason=f"{src}: peak shaving (the battery is kept for later)")
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


def winter_hold(hold: float, r: Readings, mode: str, mode_age_s: float) -> Decision | None:
    """The winter level, below `hold` + WINTER_BAND: peak shaving from the level down (the house on
    the grid, the battery only above the import limit), self-use above it until peak shaving has
    started; once started it lasts until WINTER_BAND above the level. Between self-use and peak
    shaving it waits WINTER_DWELL_S after the last mode change. None: the plan decides."""
    shave = r.soc <= hold or mode == "peak_shave"
    if mode in ("self_use", "peak_shave") and mode_age_s < WINTER_DWELL_S:
        if (mode == "peak_shave") != shave:
            return Decision("none", reason=f"winter level {hold:.0f} %: {mode.replace('_', ' ')} kept a few "
                                           "minutes before switching")
        shave = mode == "peak_shave"
    if shave:
        if mode != "peak_shave":
            return Decision("peak_shave", reason=f"winter level: below {hold:.0f} % the house runs on the grid "
                                                 "and the battery is kept (peak shaving)")
        return Decision("none", reason=f"winter level {hold:.0f} % kept (peak shaving)")
    if mode != "self_use":
        return Decision("self_use", reason=f"winter level: self-use down to {hold:.0f} %")
    return Decision("none", reason=f"self-use down to the winter level ({hold:.0f} %)")
