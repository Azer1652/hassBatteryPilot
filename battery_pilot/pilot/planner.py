"""The battery plan: what to do in each time slot of the coming day and a half.

`optimize()` is a dynamic programme over the battery SOC in 1 % steps. In each
slot the battery can only do what the Solis can be told to do:

* self_use  - cover the house deficit / store solar surplus (the inverter default);
* charge    - charge from the grid (on top of any solar surplus);
* discharge - discharge on top of the house deficit and export the rest.

The cost of a slot is what is bought minus what is sold, plus battery wear,
plus a penalty for grid import above the import limit (the capacity tariff).
Energy left at the end of the horizon is valued at what it saves later.

`make_plan()` builds the slots from the contract, prices, load and solar
forecasts, and adds the peak-shaving rule from the original playbook: on a
winter day (corrected solar forecast below the threshold) the battery must be
at the night target SOC when the night window ends, so it can carry the
house through the day and keep the quarter-hour peaks low. In winter the
battery never exports. Under a fixed day/night contract buying at night to
sell or use by day does not pay (losses + wear exceed the spread), so the plan
only grid-charges for that winter target. Under a dynamic contract it also
charges in cheap hours and exports in expensive ones when the spread pays.
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
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
    cost_c: float

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
                "allow_export": self.allow_export, "targets": self.targets,
                "steps": [s.to_json() for s in self.steps],
                "cost_eur": round(sum(s.cost_c for s in self.steps) / 100, 2)}


# ---------------------------------------------------------------- optimizer
def _slot_costs(b: Battery, s: Slot, e: float, dmin: int, dmax: int, curtail_negative: bool):
    """Cost and flows of every SOC change d (in levels) in this slot."""
    net = s.load_kwh - s.pv_kwh
    out = {}
    for d in range(dmin, dmax + 1):
        de = d * e
        if de >= 0:
            grid, dis = net + de / b.eta_c, 0.0
        else:
            grid, dis = net + de * b.eta_d, -de
        imp, exp = max(grid, 0.0), max(-grid, 0.0)
        exp = min(exp, b.export_limit_kw * s.hours)
        revenue = 0.0 if (curtail_negative and s.sell < 0) else exp * s.sell
        if curtail_negative and s.sell < 0:
            exp = 0.0
        cost = imp * s.buy - revenue + dis * b.wear_c_kwh
        over = imp / s.hours - b.import_limit_kw
        if over > 1e-9:
            cost += over * b.capacity_c_per_kw_month
        out[d] = (cost, imp, exp)
    return out


def _self_delta(b: Battery, s: Slot, e: float) -> float:
    net = s.load_kwh - s.pv_kwh
    if net > 0:
        return -min(net / b.eta_d, b.max_discharge_kw * s.hours) / e
    return min(-net * b.eta_c, b.max_charge_kw * s.hours) / e


def optimize(b: Battery, slots: list[Slot], soc0: float, targets: dict[int, float] | None = None,
             allow_export: bool = True, curtail_negative: bool = False,
             terminal_c_kwh: float = 0.0) -> list[Step]:
    """Cheapest sequence of actions. `targets[i]` = minimum SOC at the end of slot i."""
    if not slots:
        return []
    targets = targets or {}
    e = b.capacity_kwh / 100
    levels = range(101)
    smin = int(math.ceil(b.min_soc))
    sres = int(math.ceil(b.reserve_soc))
    s0 = int(min(max(round(soc0), 0), 100))
    T = len(slots)

    # value of the energy left at the end
    nxt = [-terminal_c_kwh * max(s - smin, 0) * e for s in levels]
    choice: list[list[int]] = [None] * T  # type: ignore[list-item]
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
        dself_raw = _self_delta(b, sl, e)
        cmin = max(1, math.ceil(min(MIN_CHARGE_KW, b.max_charge_kw) * sl.hours * b.eta_c / e - 1e-9))
        # Peak cover: a typical quarter-hour peak above the import limit must come out of the
        # battery, so entering the slot it needs that energy above its minimum (else: capacity cost).
        excess = sl.peak_kw - sl.pv_kwh / sl.hours - b.import_limit_kw
        need = int(math.ceil(excess * PEAK_MARGIN * 0.25 / b.eta_d / e)) + 1 if excess > 0 else 0
        exposed = excess * b.capacity_c_per_kw_month * PEAK_RISK if excess > 0 else 0.0
        cur = [0.0] * 101
        ch = [0] * 101
        for s in levels:
            lo = max(min(smin - s, 0), -maxd)
            hi = min(100 - s, maxc)
            ds = int(round(min(max(dself_raw, lo), hi)))
            best_d = ds
            best = table[ds][0] + nxt[s + ds]
            risk = exposed if s - smin < need else 0.0
            best += risk
            act = ACTION_COST_C_PER_H * sl.hours
            cap_kwh = (b.import_limit_kw - CHARGE_HEADROOM_KW) * sl.hours
            for d in range(max(ds, 0) + cmin, hi + 1):         # charge from the grid
                if table[d][1] > cap_kwh + 1e-9:
                    break                                      # more charge only imports more
                v = table[d][0] + nxt[s + d] + act + risk
                if v < best - 1e-9:
                    best, best_d = v, d
            if allow_export:
                for d in range(max(lo, sres - s), min(ds, 0)):  # discharge / export, not below reserve
                    v = table[d][0] + nxt[s + d] + act + risk
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
        ds = int(round(min(max(_self_delta(b, sl, e), lo), hi)))
        action = "self_use" if d == ds else ("charge" if d > ds else "discharge")
        cost, imp, exp = costs[t][d]
        steps.append(Step(sl.start, sl.hours, action, s, s + d, imp, exp, sl.load_kwh, sl.pv_kwh,
                          sl.buy, sl.sell, cost))
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
        we_day = day + timedelta(days=1) if o.night_start_time > o.night_end_time else day
        we = datetime.combine(we_day, o.night_end_time, tz)
        de = datetime.combine(we_day, o.night_start_time, tz)
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
              forecast_until: datetime | None = None, peak_kw: LoadFn | None = None) -> Plan:
    """`forecast_until`: end of the solar forecast; nights after it get no winter target."""
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
        idx = max((i for i, s in enumerate(slots) if s.end <= we + timedelta(seconds=1)), default=None)
        info = {"window_start": ws.isoformat(), "window_end": we.isoformat(), "solar_kwh": round(solar, 1),
                "need_kwh": need_kwh, "soc": soc_needed, "winter": is_winter}
        # Forecasts list daylight hours only, so a day counts as forecast once the forecast
        # reaches past its late morning.
        known = forecast_until is None or forecast_until >= we + timedelta(hours=4)
        info["forecast"] = known
        target_info.append(info)
        if len(target_info) == 1:
            winter = is_winter and known   # the first upcoming night decides winter behaviour now
        if is_winter and known and idx is not None and o.winter_mode == "full_day":
            targets[idx] = soc_needed

    buys = [s.buy for s in slots] or [0.0]
    terminal = max(min(statistics.median(buys), statistics.fmean(buys)) * b.eta_d - b.wear_c_kwh, 0.0)
    allow_export = not (winter and o.winter_mode == "full_day")
    steps = optimize(b, slots, soc, targets, allow_export, curtail_negative, terminal)
    return Plan(steps, now, tariff.kind, target_info, winter, allow_export)


def in_night_window(t_local: datetime, o: Options) -> bool:
    """The clock window for night charging (weekends included, unlike the tariff)."""
    return in_window(t_local.time(), o.night_start_time, o.night_end_time)


__all__ = ["Slot", "Step", "Plan", "optimize", "make_plan", "night_target", "night_windows",
           "slot_grid", "market_at", "is_night", "in_night_window"]
