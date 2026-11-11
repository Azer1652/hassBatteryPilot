"""The battery plan: what to do in each time slot of the coming day and a half.

`optimize()` is a dynamic programme over the battery SOC in 1 % steps. In each
slot the battery can only do what the Solis can be told to do:

* self_use  - cover the house deficit / store solar surplus (the inverter default);
* charge    - charge from the grid (on top of any solar surplus);
* discharge - discharge on top of the house deficit and export the rest.

The cost of a slot is what is bought minus what is sold, plus battery wear,
plus a penalty for grid import above the import limit (the capacity tariff).
Energy left at the end of the horizon is valued at what it saves later.
Charging and discharging move whole SOC points; self-use moves a fraction of
one per slot, so its rounding error is carried from slot to slot and its
grid flows are those of the exact change.

`make_plan()` builds the slots from the contract, prices, load and solar
forecasts, and adds the peak-shaving rule from the original playbook: on a
winter day (corrected solar forecast below the threshold) the battery must be
at the night target SOC when the night window ends, so it can carry the
house through the day and keep the quarter-hour peaks low. In winter the
battery does not export (except in extreme slots, below). Under a fixed day/night contract buying at night to
sell or use by day does not pay (losses + wear exceed the spread), so the plan
only grid-charges for that winter target. Under a dynamic contract it also
charges in cheap hours and exports in expensive ones when the spread pays.

Two rules give way when the money clearly outweighs them:

* the import limit: a second plan may charge above it when what it earns beats
  the higher capacity peak, priced the Fluvius way (the month's highest
  quarter-hour, at least the billed minimum, counts once in the 12-month
  average);
* the winter no-export rule and the battery reserve: in "extreme" slots, where
  selling pays even if the energy must be bought back at the dearest later
  price, the battery may be sold down to its minimum.
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Options
from .prices import PriceSlot
from .simulate import Battery
from .simulate import step as sim_step
from .tariffs import Tariff, in_window, is_night

TARGET_PENALTY_C = 500.0   # per missing SOC point at a target: effectively a hard rule
# Grid charging only at a real rate (the old playbook paused below 10 A). Without this the
# optimizer "holds" the battery with a trickle charge to save 1-2 c/kWh of losses, and a
# held battery does not cover the cooking peaks that the capacity tariff bills.
MIN_CHARGE_KW = 0.5
# Cost per hour of charge or discharge instead of self-use: keeps the plan from switching the
# inverter for gains of a cent or two (forecast errors are larger than that).
ACTION_COST_C_PER_H = 2.0
# Grid charging is planned this far under the import limit: the controller follows the house
# load in 5 A steps and pauses on quarter-hour spikes, so it cannot charge right up to the limit.
CHARGE_HEADROOM_KW = 0.3
# Share of a month's capacity cost charged for each slot in which a typical house peak would hit
# the grid because the battery is empty. The bill takes the month's highest quarter-hour, so one
# miss costs the full amount; this is how much the plan pays to avoid the risk on one day.
PEAK_RISK = 0.5
PEAK_MARGIN = 1.3   # real peaks vary around the learned typical one
# Charging above the import limit: tried in steps of this size, and only adopted when it beats the
# normal plan by this much after the capacity cost (forecasts and prices are not exact).
BOOST_STEP_KW = 0.5
BOOST_MIN_GAIN_C = 25.0
# A slot is "extreme" when selling a stored kWh beats buying it back at the dearest later price by this.
EXTREME_MARGIN_C = 1.0
# The plan is redone every few minutes from the measured SOC. Changing what the inverter does right now
# must be worth this much, or the last few SOC points flip it back and forth (each flip is a full
# command sequence to the inverter).
SWITCH_COST_C = 5.0


@dataclass
class Slot:
    start: datetime          # aware, UTC
    hours: float
    load_kwh: float
    pv_kwh: float
    buy: float               # c/kWh
    sell: float              # c/kWh
    market: float | None = None   # EUR/MWh, None when estimated or not used
    peak_kw: float = 0.0          # typical highest quarter-hour house load in this slot

    @property
    def end(self) -> datetime:
        return self.start + timedelta(hours=self.hours)


@dataclass
class Step:
    start: datetime
    hours: float
    action: str              # self_use | charge | discharge
    soc_start: float
    soc_end: float
    import_kwh: float
    export_kwh: float
    load_kwh: float
    pv_kwh: float
    buy: float
    sell: float
    cost_c: float                    # incl. capacity_c
    capacity_c: float = 0.0          # the part of cost_c for import above the import limit
    limit_kw: float | None = None    # import level charging may follow (None: the import limit setting)
    reserve: float | None = None     # export floor (None: the battery reserve setting)

    def to_json(self) -> dict:
        d = asdict(self)
        d["start"] = self.start.isoformat()
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in d.items()}


@dataclass
class Plan:
    steps: list[Step]
    created: datetime
    contract: str
    targets: list[dict] = field(default_factory=list)   # [{"at": iso, "soc": n, "solar_kwh": x}]
    winter: bool = False
    allow_export: bool = True
    boost: dict | None = None        # charging above the import limit: {limit_kw, from, to, peak_cost_eur, gain_eur}

    @property
    def now(self) -> Step | None:
        return self.steps[0] if self.steps else None

    def step_at(self, t: datetime) -> Step | None:
        for s in self.steps:
            if s.start <= t < s.start + timedelta(hours=s.hours):
                return s
        return None

    def to_json(self) -> dict:
        return {"created": self.created.isoformat(), "contract": self.contract, "winter": self.winter,
                "allow_export": self.allow_export, "targets": self.targets, "boost": self.boost,
                "steps": [s.to_json() for s in self.steps],
                "cost_eur": round(sum(s.cost_c for s in self.steps) / 100, 2)}


# ---------------------------------------------------------------- optimizer
def _flow(b: Battery, s: Slot, de: float, curtail_negative: bool) -> tuple[float, float, float, float]:
    """(cost incl. capacity, import kWh, export kWh, capacity cost) of a battery change of `de` kWh in this slot."""
    net = s.load_kwh - s.pv_kwh
    if de >= 0:
        grid, dis = net + de / b.eta_c, 0.0
    else:
        grid, dis = net + de * b.eta_d, -de
    imp, exp = (grid, 0.0) if grid > 0 else (0.0, -grid if grid < 0 else 0.0)
    exp = min(exp, b.export_limit_kw * s.hours)
    revenue = 0.0 if (curtail_negative and s.sell < 0) else exp * s.sell
    if curtail_negative and s.sell < 0:
        exp = 0.0
    cost = imp * s.buy - revenue + dis * b.wear_c_kwh
    cap = 0.0
    over = imp / s.hours - b.import_limit_kw
    if over > 1e-9:
        cap = over * b.capacity_c_per_kw_month
    return cost + cap, imp, exp, cap


def _slot_costs(b: Battery, s: Slot, e: float, dmin: int, dmax: int, curtail_negative: bool):
    """Cost and flows of every SOC change d (in levels) in this slot."""
    return {d: _flow(b, s, d * e, curtail_negative) for d in range(dmin, dmax + 1)}


def _self_delta(b: Battery, s: Slot, e: float) -> float:
    net = s.load_kwh - s.pv_kwh
    if net > 0:
        return -min(net / b.eta_d, b.max_discharge_kw * s.hours) / e
    return min(-net * b.eta_c, b.max_charge_kw * s.hours) / e


def optimize(b: Battery, slots: list[Slot], soc0: float, targets: dict[int, float] | None = None,
             allow_export: bool = True, curtail_negative: bool = False,
             terminal_c_kwh: float = 0.0, extreme: frozenset[int] = frozenset(),
             first_action: str | None = None) -> list[Step]:
    """Cheapest sequence of actions. `targets[i]` = minimum SOC at the end of slot i.

    In the `extreme` slots the battery may be exported even when `allow_export` is False,
    and down to its minimum instead of the reserve. `first_action`: what is being done now;
    doing something else in the first slot costs SWITCH_COST_C."""
    if not slots:
        return []
    targets = targets or {}
    e = b.capacity_kwh / 100
    levels = range(101)
    smin = int(math.ceil(b.min_soc))
    sres = int(math.ceil(b.reserve_soc))
    s0 = int(min(max(round(soc0), 0), 100))
    T = len(slots)

    # Self-use moves the battery by a fraction of a SOC point per slot. Rounded slot by slot,
    # the error always goes the same way (0.30 kW for a quarter-hour is 0.49 of a point: never
    # a change; 0.35 kW is always a whole one) and the plan drifts away from the battery. So
    # the rounding error is carried to the next slot (`whole`), self-use is costed with the
    # flows of the exact change, and the value of what follows is taken between the two SOC
    # points around it.
    exact = [_self_delta(b, sl, e) for sl in slots]
    whole: list[int] = []
    acc, done = 0.0, 0
    for x in exact:
        acc += x
        whole.append(int(round(acc)) - done)
        done += whole[-1]

    # value of the energy left at the end
    nxt = [-terminal_c_kwh * max(s - smin, 0) * e for s in levels]
    choice: list[list[int | None]] = [None] * T  # type: ignore[list-item]   # None: self-use
    costs: list[dict] = [None] * T  # type: ignore[list-item]
    for t in range(T - 1, -1, -1):
        sl = slots[t]
        if t in targets:
            r = targets[t]
            nxt = [v + TARGET_PENALTY_C * max(r - s, 0) for s, v in zip(levels, nxt)]
        maxc = int(b.max_charge_kw * sl.hours / e + 1e-9)
        maxd = int(b.max_discharge_kw * sl.hours / e + 1e-9)
        table = _slot_costs(b, sl, e, -maxd, maxc, curtail_negative)
        costs[t] = table
        self_cost: dict[float, float] = {}
        cmin = max(1, math.ceil(min(MIN_CHARGE_KW, b.max_charge_kw) * sl.hours * b.eta_c / e - 1e-9))
        # Peak cover: a typical quarter-hour peak above the import limit must come out of the
        # battery, so entering the slot it needs that energy above its minimum (else: capacity cost).
        excess = sl.peak_kw - sl.pv_kwh / sl.hours - b.import_limit_kw
        need = int(math.ceil(excess * PEAK_MARGIN * 0.25 / b.eta_d / e)) + 1 if excess > 0 else 0
        exposed = excess * b.capacity_c_per_kw_month * PEAK_RISK if excess > 0 else 0.0
        cur = [0.0] * 101
        ch: list[int | None] = [None] * 101

        def switch(action, t=t):
            return SWITCH_COST_C if t == 0 and first_action is not None and action != first_action else 0.0
        sw_self, sw_charge, sw_dis = switch("self_use"), switch("charge"), switch("discharge")
        for s in levels:
            lo = max(min(smin - s, 0), -maxd)
            hi = min(100 - s, maxc)
            ds = min(max(whole[t], lo), hi)           # SOC points self-use moves in this slot
            x = min(max(exact[t], lo), hi)            # the change its flows are counted for
            if x not in self_cost:
                self_cost[x] = _flow(b, sl, x * e, curtail_negative)[0]
            below = math.floor(x)
            part = x - below
            best_d = None
            best = self_cost[x] + (nxt[s + below] if part < 1e-9
                                   else (1 - part) * nxt[s + below] + part * nxt[s + below + 1])
            risk = exposed if s - smin < need else 0.0
            best += risk + sw_self
            act = ACTION_COST_C_PER_H * sl.hours
            cap_kwh = (b.import_limit_kw - CHARGE_HEADROOM_KW) * sl.hours
            for d in range(max(ds, 0) + cmin, hi + 1):         # charge from the grid
                if table[d][1] > cap_kwh + 1e-9:
                    break                                      # more charge only imports more
                v = table[d][0] + nxt[s + d] + act + risk + sw_charge
                if v < best - 1e-9:
                    best, best_d = v, d
            if allow_export or t in extreme:
                floor = smin if t in extreme else sres
                for d in range(max(lo, floor - s), min(ds, 0)):  # discharge / export, not below the floor
                    v = table[d][0] + nxt[s + d] + act + risk + sw_dis
                    if v < best - 1e-9:
                        best, best_d = v, d
            cur[s], ch[s] = best, best_d
        choice[t] = ch
        nxt = cur

    steps = []
    s = s0
    for t, sl in enumerate(slots):
        d = choice[t][s]
        lo = max(min(smin - s, 0), -int(b.max_discharge_kw * sl.hours / e + 1e-9))
        hi = min(100 - s, int(b.max_charge_kw * sl.hours / e + 1e-9))
        ds = min(max(whole[t], lo), hi)
        if d is None:
            action, d = "self_use", ds
            cost, imp, exp, cap = _flow(b, sl, min(max(exact[t], lo), hi) * e, curtail_negative)
        else:
            action = "charge" if d > ds else "discharge"
            cost, imp, exp, cap = costs[t][d]
        reserve = b.min_soc if (action == "discharge" and t in extreme and s + d < b.reserve_soc) else None
        steps.append(Step(sl.start, sl.hours, action, s, s + d, imp, exp, sl.load_kwh, sl.pv_kwh,
                          sl.buy, sl.sell, cost, cap, None, reserve))
        s += d
    return steps


# ---------------------------------------------------------------- building the slots
LoadFn = Callable[[datetime, datetime], float]   # kWh the house uses between two UTC times
PvFn = Callable[[datetime, datetime], float]     # corrected solar kWh between two UTC times


def slot_grid(now: datetime, end: datetime, tz: ZoneInfo, minutes: int) -> list[tuple[datetime, datetime]]:
    """Slots from now to end, aligned to `minutes` in local time; the first one is partial."""
    local = now.astimezone(tz)
    floor = local.replace(minute=(local.minute // minutes) * minutes, second=0, microsecond=0)
    out = []
    t = now
    nxt = (floor + timedelta(minutes=minutes)).astimezone(timezone.utc)
    while t < end:
        stop = min(nxt, end)
        if (stop - t).total_seconds() >= 60:
            out.append((t, stop))
        t, nxt = stop, nxt + timedelta(minutes=minutes)
    return out


def market_at(price_slots: list[PriceSlot], t: datetime) -> tuple[float | None, bool]:
    """Market price at t; past the known prices, the price 24 h (or 48 h) earlier. (price, known)"""
    for back in (0, 1, 2):
        tt = t - timedelta(days=back)
        for p in price_slots:
            if p.start <= tt < p.end:
                return p.eur_mwh, back == 0
    if price_slots:
        return statistics.fmean(p.eur_mwh for p in price_slots), False
    return None, False


def night_windows(start: datetime, end: datetime, tz: ZoneInfo, o: Options):
    """(window start, window end, next day end) in UTC for windows that end in [start, end]."""
    out = []
    day = start.astimezone(tz).date() - timedelta(days=1)
    last = end.astimezone(tz).date() + timedelta(days=1)
    while day <= last:
        ws = datetime.combine(day, o.night_start_time, tz)
        wraps = o.night_start_time > o.night_end_time          # the window crosses midnight
        we_day = day + timedelta(days=1) if wraps else day
        we = datetime.combine(we_day, o.night_end_time, tz)
        # The day it must carry ends at the next night start: the same date for a window that
        # crosses midnight, the next date for one inside a day (01:00-06:00).
        de = datetime.combine(we_day if wraps else we_day + timedelta(days=1), o.night_start_time, tz)
        if start < we.astimezone(timezone.utc) <= end:
            out.append((ws.astimezone(timezone.utc), we.astimezone(timezone.utc), de.astimezone(timezone.utc)))
        day += timedelta(days=1)
    return out


def night_target(b: Battery, o: Options, day: list[tuple[float, float, float]]) -> tuple[int, float]:
    """SOC needed when the night window ends so self-use carries the house until the next night.

    `day` = [(load_kwh, pv_kwh, hours)] from window end to the next window start.
    Returns (target SOC incl. safety margin, kWh the battery must supply).
    """
    def bought(s0: float) -> float:
        soc, total = s0, 0.0
        for load, pv, h in day:
            r = sim_step(b, soc, "self_use", 0, load, pv, h)
            soc, total = r.soc, total + r.import_kwh
        return total

    unavoidable = bought(100) + 0.05      # e.g. load above the battery's power
    lo, hi = int(math.ceil(b.min_soc)), 100
    while lo < hi:
        mid = (lo + hi) // 2
        if bought(mid) <= unavoidable:
            hi = mid
        else:
            lo = mid + 1
    need_soc = lo
    need_kwh = b.kwh(need_soc - b.min_soc)
    target = min(100, need_soc + o.safety_margin_pct)
    return int(5 * math.ceil(target / 5)), round(need_kwh, 2)


def make_plan(now: datetime, tz: ZoneInfo, o: Options, tariff: Tariff, price_slots: list[PriceSlot],
              load_kwh: LoadFn, pv_kwh: PvFn, soc: float, market_avg: float | None = None,
              curtail_negative: bool = False, horizon_h: int = 36,
              forecast_until: datetime | None = None, peak_kw: LoadFn | None = None,
              month_peak_kw: float = 0.0, current_action: str | None = None) -> Plan:
    """`forecast_until`: end of the solar forecast; nights after it get no winter target.
    `month_peak_kw`: the highest quarter-hour import so far this month (for the capacity cost).
    `current_action`: what the previous plan is still doing now (see `sticky_action`)."""
    b = Battery.from_options(o)
    end = now + timedelta(hours=horizon_h)
    minutes = 60
    if tariff.kind == "dynamic" and price_slots and min(p.minutes for p in price_slots) <= 15:
        minutes = 15
    slots: list[Slot] = []
    for t0, t1 in slot_grid(now, end, tz, minutes):
        market, _known = market_at(price_slots, t0)
        mid_local = (t0 + (t1 - t0) / 2).astimezone(tz)
        h = (t1 - t0).total_seconds() / 3600
        slots.append(Slot(t0, h, load_kwh(t0, t1), pv_kwh(t0, t1), tariff.buy(mid_local, market),
                          tariff.sell(mid_local, market, market_avg), market,
                          peak_kw(t0, t1) if peak_kw else 0.0))

    if len(slots) > 1 and slots[0].hours < minutes / 60 / 3 \
            and abs(slots[0].buy - slots[1].buy) < 1e-9 and abs(slots[0].sell - slots[1].sell) < 1e-9:
        # A few minutes before the boundary at the same price: merge into the next slot, so a replan
        # just before the hour does not plan a 5-minute self-use gap between two charging hours.
        a, z = slots[0], slots[1]
        slots[1] = Slot(a.start, a.hours + z.hours, a.load_kwh + z.load_kwh, a.pv_kwh + z.pv_kwh, z.buy, z.sell,
                        z.market, max(a.peak_kw, z.peak_kw))
        slots.pop(0)

    targets: dict[int, float] = {}
    target_info = []
    winter = False
    for ws, we, de in night_windows(now, end, tz, o):
        day = []
        t = we
        while t < de:
            t2 = min(t + timedelta(hours=1), de)
            day.append((load_kwh(t, t2), pv_kwh(t, t2), (t2 - t).total_seconds() / 3600))
            t = t2
        solar = sum(p for _, p, _ in day)
        is_winter = solar < o.winter_solar_threshold_kwh
        soc_needed, need_kwh = night_target(b, o, day)
        # Never ask for more than the night window can charge from the minimum under the import
        # limit: an unreachable target makes the plan buy at the day rate before the window opens.
        reachable = 0          # SOC points, counted per hour in whole points as the optimizer does
        t = ws
        while t < we:
            t2 = min(t + timedelta(hours=1), we)
            h = (t2 - t).total_seconds() / 3600
            room_kw = b.import_limit_kw - CHARGE_HEADROOM_KW - load_kwh(t, t2) / h
            reachable += int(min(max(room_kw, 0.0) * b.eta_c, b.max_charge_kw) * h / (b.capacity_kwh / 100) + 1e-9)
            t = t2
        cap = int(5 * math.floor((b.min_soc + reachable) / 5))
        capped = soc_needed > cap
        soc_needed = min(soc_needed, max(cap, int(math.ceil(b.min_soc))))
        idx = max((i for i, s in enumerate(slots) if s.end <= we + timedelta(seconds=1)), default=None)
        info = {"window_start": ws.isoformat(), "window_end": we.isoformat(), "solar_kwh": round(solar, 1),
                "need_kwh": need_kwh, "soc": soc_needed, "winter": is_winter, "capped": capped}
        # Forecasts list daylight hours only, so a day counts as forecast once the forecast
        # reaches past its late morning.
        known = forecast_until is None or forecast_until >= we + timedelta(hours=4)
        info["forecast"] = known
        info["applied"] = bool(is_winter and known and idx is not None and o.winter_mode == "full_day")
        info["winter_mode"] = o.winter_mode
        target_info.append(info)
        if len(target_info) == 1:
            winter = is_winter and known   # the first upcoming night decides winter behaviour now
        if info["applied"]:
            targets[idx] = soc_needed

    buys = [s.buy for s in slots] or [0.0]
    terminal = max(min(statistics.median(buys), statistics.fmean(buys)) * b.eta_d - b.wear_c_kwh, 0.0)
    allow_export = not (winter and o.winter_mode == "full_day")
    extreme = extreme_slots(b, slots)
    steps = optimize(b, slots, soc, targets, allow_export, curtail_negative, terminal, extreme, current_action)
    boost = None
    if o.capacity_tariff_eur_kw_year > 0 and steps:
        steps, boost = _boost(b, o, slots, soc, targets, allow_export, curtail_negative, terminal, extreme,
                              steps, now, tz, month_peak_kw, current_action)
    return Plan(steps, now, tariff.kind, target_info, winter, allow_export, boost)


def sticky_action(prev: Plan | None, t: datetime, soc: float, reserve: float) -> str | None:
    """What the previous plan is doing at `t` and has not finished: self-use, or a charge or
    discharge that has not reached its SOC yet. A finished step is not sticky, so a plan never
    keeps charging past a target it reached."""
    step = prev.step_at(t) if prev else None
    if step is None:
        return None
    if step.action == "charge":
        return "charge" if soc < step.soc_end - 0.5 else None
    if step.action == "discharge":
        floor = step.reserve if step.reserve is not None else reserve
        return "discharge" if soc > max(step.soc_end, floor) + 0.5 else None
    return "self_use"


def step_to_follow(plan: Plan | None, t: datetime, b: Battery, mode: str) -> Step | None:
    """The step the controller follows at `t`, given what the inverter is doing (`mode`).

    That is the plan's step at `t`, with one exception. The plan is redone every few minutes,
    and the minutes left before a slot boundary are a first slot of their own. When that slot
    is too short to charge (or discharge) one whole SOC point, the optimizer can only put
    self-use there. If the inverter is charging and the plan charges on right after, stopping
    for those minutes and starting again costs charging time and a full command sequence, at
    every boundary. The next step then counts from now, so the pace to its target carries on.
    It never starts anything: with the inverter in self-use the short slot stays self-use."""
    step = plan.step_at(t) if plan else None
    if step is None or step.action != "self_use" or mode not in ("charge", "discharge"):
        return step
    i = plan.steps.index(step)
    nxt = plan.steps[i + 1] if i + 1 < len(plan.steps) else None
    if nxt is None or nxt.action != mode:
        return step
    e = b.capacity_kwh / 100
    if mode == "charge":
        limit = nxt.limit_kw if nxt.limit_kw is not None else b.import_limit_kw
        room_kwh = min((limit - CHARGE_HEADROOM_KW) * step.hours - (step.load_kwh - step.pv_kwh),
                       b.max_charge_kw * step.hours / b.eta_c)
        too_short = room_kwh * b.eta_c < e
    else:
        too_short = b.max_discharge_kw * step.hours < e
    if not too_short:
        return step
    return replace(nxt, start=step.start, hours=step.hours + nxt.hours, soc_start=step.soc_start)


def extreme_slots(b: Battery, slots: list[Slot]) -> frozenset[int]:
    """Slots where selling a stored kWh pays even if it has to be bought back at the dearest later price."""
    out = set()
    overall = max(s.buy for s in slots) if slots else 0.0
    later = None
    for t in range(len(slots) - 1, -1, -1):
        ref = later if later is not None else overall
        if slots[t].sell * b.eta_d - b.wear_c_kwh > ref / b.eta_c + EXTREME_MARGIN_C:
            out.add(t)
        later = slots[t].buy if later is None else max(later, slots[t].buy)
    return frozenset(out)


# ---------------------------------------------------------------- above the import limit
def _peak_cost_c(b: Battery, steps: list[Step], tz: ZoneInfo, month_now: str, free_now: float,
                 free_later: float) -> float:
    """Capacity cost of a plan, the way it is billed: per month, the highest quarter-hour above what
    that month already costs (charging may run up to the limit the controller follows)."""
    peaks: dict[str, float] = {}
    for s in steps:
        kw = s.import_kwh / s.hours
        if s.action == "charge":
            kw = max(kw, s.limit_kw if s.limit_kw is not None else b.import_limit_kw)
        key = s.start.astimezone(tz).strftime("%Y-%m")
        peaks[key] = max(peaks.get(key, 0.0), kw)
    return sum(max(kw - (free_now if key == month_now else free_later), 0.0)
               for key, kw in peaks.items()) * b.capacity_c_per_kw_month


def _money_c(b: Battery, steps: list[Step], terminal_c_kwh: float) -> float:
    """What a plan costs in money, without the capacity cost: energy, wear, minus the energy left."""
    left = max(steps[-1].soc_end - b.min_soc, 0.0) * b.capacity_kwh / 100
    return sum(s.cost_c - s.capacity_c for s in steps) - terminal_c_kwh * left


def _boost(b: Battery, o: Options, slots: list[Slot], soc: float, targets: dict, allow_export: bool,
           curtail_negative: bool, terminal: float, extreme: frozenset[int], normal: list[Step],
           now: datetime, tz: ZoneInfo, month_peak_kw: float,
           current_action: str | None = None) -> tuple[list[Step], dict | None]:
    """Try charging above the import limit; keep it only when it beats the normal plan after the capacity cost."""
    free_now = max(month_peak_kw, o.capacity_min_kw)
    free_later = o.capacity_min_kw
    month_now = now.astimezone(tz).strftime("%Y-%m")
    rate = b.capacity_c_per_kw_month
    limit = b.import_limit_kw
    top = b.max_charge_kw / b.eta_c + max(s.load_kwh / s.hours for s in slots) + CHARGE_HEADROOM_KW
    levels = {round(limit + BOOST_STEP_KW * k, 3) for k in range(1, int((top - limit) / BOOST_STEP_KW) + 2)}
    if free_now > limit + 0.05:
        levels.add(round(free_now, 3))
    # Upper bound of what one more kWh charged in a slot can be worth later (sold, or not bought).
    best = max(max(s.buy for s in slots), max(s.sell for s in slots), terminal / b.eta_d)
    worth = b.eta_c * b.eta_d * best - b.eta_c * b.wear_c_kwh
    # Full charging power in the slots where it is worth most: as fast as the battery can.
    for s in sorted(slots, key=lambda s: s.buy)[:3]:
        full = s.load_kwh / s.hours + b.max_charge_kw / b.eta_c + CHARGE_HEADROOM_KW
        if full > limit + 0.05:
            levels.add(round(full, 2))
    hours = sorted(((worth - s.buy, s.hours) for s in slots if worth - s.buy > 0), reverse=True)
    room_kwh = b.capacity_kwh / b.eta_c

    score_normal = _money_c(b, normal, terminal) + _peak_cost_c(b, normal, tz, month_now, free_now, free_later)
    best_plan, best_score, best_level = normal, score_normal, None
    for level in sorted(levels) if hours else []:
        extra_kw = min(level - limit, b.max_charge_kw / b.eta_c)
        bound, left = 0.0, room_kwh
        for g, h in hours:
            e = min(extra_kw * h, left)
            bound, left = bound + g * e, left - e
            if left <= 0:
                break
        if bound - max(level - free_now, 0.0) * rate < BOOST_MIN_GAIN_C:
            continue
        steps = optimize(replace(b, import_limit_kw=level), slots, soc, targets, allow_export, curtail_negative,
                         terminal, extreme, current_action)
        cap = limit - CHARGE_HEADROOM_KW
        for st in steps:                # only the steps that charge above the normal limit follow the higher one
            if st.action == "charge" and st.import_kwh / st.hours > cap + 0.05:
                st.limit_kw = level
        if all(st.limit_kw is None for st in steps):
            continue                    # nothing charges above the limit at this level: not a boost
        score = _money_c(b, steps, terminal) + _peak_cost_c(b, steps, tz, month_now, free_now, free_later)
        if score < best_score - 1e-9:
            best_plan, best_score, best_level = steps, score, level
    if best_level is None or best_score > score_normal - BOOST_MIN_GAIN_C:
        return normal, None
    boosted = [s for s in best_plan if s.limit_kw is not None]
    if not boosted:
        return normal, None
    peak_cost = (_peak_cost_c(b, best_plan, tz, month_now, free_now, free_later)
                 - _peak_cost_c(b, normal, tz, month_now, free_now, free_later))
    gain = score_normal - best_score + peak_cost
    return best_plan, {"limit_kw": best_level, "from": boosted[0].start.isoformat(),
                       "to": (boosted[-1].start + timedelta(hours=boosted[-1].hours)).isoformat(),
                       "peak_cost_eur": round(peak_cost / 100, 2), "gain_eur": round(gain / 100, 2)}


def in_night_window(t_local: datetime, o: Options) -> bool:
    """The clock window for night charging (weekends included, unlike the tariff)."""
    return in_window(t_local.time(), o.night_start_time, o.night_end_time)


__all__ = ["Slot", "Step", "Plan", "optimize", "make_plan", "night_target", "night_windows",
           "slot_grid", "market_at", "is_night", "in_night_window", "extreme_slots", "sticky_action",
           "step_to_follow"]
