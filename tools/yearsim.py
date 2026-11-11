"""A synthetic year for Battery Pilot: does it do the right things, and what does each contract cost?

The house: 6 kWp of panels near Antwerp, trees that shade the panels when the
winter sun is low (true shade the app does not know and must learn), about
13-15 kWh a day with a morning bump, an evening peak and a short cooking
spike, a 16 kWh battery. Prices: a seasonal market level with morning and
evening peaks, a solar dip at noon that turns negative on sunny summer days,
and noise. The weather is random but repeatable (seed).

Every day is replayed for both contracts with `compare.replay_day` (the same
code the app runs every night): the planner decides every hour with the
forecast it would have had, the battery simulator applies its commands to the
"measured" load and solar. The app's shade and load models learn day by day,
as in the real app.

    python tools/yearsim.py            # the whole year, monthly table
    python tools/yearsim.py --days 30  # a shorter run
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "battery_pilot"))

from pilot.compare import replay_day  # noqa: E402
from pilot.config import Options  # noqa: E402
from pilot.load import LoadProfile  # noqa: E402
from pilot.meter import QuarterRecord  # noqa: E402
from pilot.prices import PriceSlot  # noqa: E402
from pilot.solar import PvForecast, ShadeModel, hourly_from_daily  # noqa: E402
from pilot.tariffs import capacity_month_eur, is_night, tariff  # noqa: E402

TZ = ZoneInfo("Europe/Brussels")
LAT, LON = 51.2, 4.4
KWP = 6.0
MARKET_BY_MONTH = [120, 110, 95, 80, 70, 65, 70, 75, 90, 105, 115, 125]   # EUR/MWh


def true_shade(month: int, hour: int) -> float:
    """Trees: low winter sun is blocked in the morning and afternoon."""
    if month in (11, 12, 1, 2):
        return 0.3 if hour < 11 or hour >= 14 else 0.9
    if month in (3, 10):
        return 0.6 if hour < 10 or hour >= 15 else 1.0
    return 1.0


@dataclass
class World:
    seed: int = 1
    price_scale: float = 1.0
    rnd: random.Random = field(init=False)
    weather: dict = field(default_factory=dict)

    def __post_init__(self):
        self.rnd = random.Random(self.seed)

    def cloud(self, d: date) -> float:
        """Forecast weather factor for a day (0 = dark, 1 = clear)."""
        if d not in self.weather:
            winter = d.month in (11, 12, 1, 2)
            self.weather[d] = min(max(self.rnd.betavariate(2, 3.5) if winter else self.rnd.betavariate(3.5, 2), 0.05), 1)
        return self.weather[d]

    def clear_kwh(self, d: date) -> float:
        doy = d.timetuple().tm_yday
        return KWP * (3.5 + 2.6 * math.cos(2 * math.pi * (doy - 172) / 365))

    def forecast(self, d: date) -> dict[datetime, float]:
        return hourly_from_daily(d, self.clear_kwh(d) * self.cloud(d), LAT, LON, TZ)

    def load_kw(self, t_local: datetime) -> float:
        h = t_local.hour + t_local.minute / 60
        kw = 0.38 + (0.15 if t_local.month in (11, 12, 1, 2) else 0)
        if 7 <= h < 8.5:
            kw += 0.6
        if 17 <= h < 21.5:
            kw += 0.9
        if 18 <= h < 18.25:
            kw += 2.4                     # cooking
        if t_local.weekday() >= 5 and 10 <= h < 16:
            kw += 0.4
        return kw

    def prices(self, d: date) -> list[PriceSlot]:
        rnd = random.Random(self.seed * 1000 + d.toordinal())
        base = MARKET_BY_MONTH[d.month - 1]
        sun = self.cloud(d) * self.clear_kwh(d) / (KWP * 6.1)
        out = []
        t = datetime.combine(d, time(0), TZ).astimezone(timezone.utc)
        end = datetime.combine(d + timedelta(days=1), time(0), TZ).astimezone(timezone.utc)
        while t < end:                       # 23 or 25 hours on clock-change days
            h = t.astimezone(TZ).hour
            p = base
            if 7 <= h < 9:
                p += 35
            if 17 <= h < 21:
                p += 60
            if h < 6:
                p -= 15
            if 10 <= h < 17:
                p -= base * 1.6 * sun * math.sin(math.pi * (h - 9.5) / 7.5)
            p = (p + rnd.uniform(-10, 10)) * self.price_scale
            out.append(PriceSlot(t, t + timedelta(hours=1), round(p, 1)))
            t += timedelta(hours=1)
        return out

    def day_records(self, d: date, fc: dict[datetime, float]) -> list[QuarterRecord]:
        rnd = random.Random(self.seed * 7919 + d.toordinal())
        start = datetime.combine(d, time(0), TZ).astimezone(timezone.utc)
        end = datetime.combine(d + timedelta(days=1), time(0), TZ).astimezone(timezone.utc)
        recs = []
        for q in range(int((end - start).total_seconds() // 900)):
            t = start + timedelta(minutes=15 * q)
            local = t.astimezone(TZ)
            hour = t.replace(minute=0)
            pv = fc.get(hour, 0.0) / 4 * true_shade(local.month, local.hour) * min(max(rnd.gauss(1, 0.15), 0.3), 1.4)
            load = self.load_kw(local) / 4 * min(max(rnd.gauss(1, 0.1), 0.6), 1.5)
            recs.append(QuarterRecord(t, {"import": 0.0, "export": 0.0, "load": load, "pv": pv},
                                      {"import": 1.0, "export": 1.0, "load": 1.0, "pv": 1.0}, None))
        return recs


def run(start: date, days: int, opts: Options | None = None, seed: int = 1, price_scale: float = 1.0,
        replan_every_h: int = 1, learn: bool = True, warmup_days: int = 7, block_negative: bool = False,
        peak_shave: bool = False, solar_hold: bool = False) -> dict:
    """`warmup_days` before `start` are simulated for learning only (like a first install's history).
    `block_negative`: as with the *block negative export* switch on.
    `peak_shave`: the inverter has a peak-shaving switch. `solar_hold`: a feed-in priority switch."""
    o = opts or Options()
    w = World(seed, price_scale)
    shade, load = ShadeModel(), LoadProfile()
    soc = {"fixed": 50.0, "dynamic": 50.0}
    months: dict[str, dict] = {}
    for i in range(-warmup_days, days):
        d = start + timedelta(days=i)
        if i < 0:
            fc = w.forecast(d)
            recs = w.day_records(d, fc)
            load.learn_records(recs, TZ)
            continue
        fc = {**w.forecast(d), **w.forecast(d + timedelta(days=1))}
        prices = w.prices(d) + w.prices(d + timedelta(days=1))
        recs = w.day_records(d, fc)
        key = d.strftime("%Y-%m")
        m = months.setdefault(key, {"days": 0, "pv": 0.0, "pv_fc": 0.0, "load": 0.0, "market": [],
                                    **{k: {"cost_c": 0.0, "imp": 0.0, "exp": 0.0, "charged": 0.0, "discharged": 0.0,
                                           "grid_charged": 0.0, "peak_kw": 0.0} for k in ("fixed", "dynamic")},
                                    "nobat": {"fixed_c": 0.0, "dynamic_c": 0.0, "peak_kw": 0.0},
                                    # what the meter would show on the fixed contract ("same usage" view)
                                    "same": {"imp_day": 0.0, "imp_night": 0.0, "exp": 0.0, "dyn_c": 0.0}})
        today = [p.eur_mwh for p in prices if p.start.astimezone(TZ).date() == d]
        avg = sum(today) / len(today)
        pv_fc = PvForecast(fc, shade, TZ).kwh
        for kind in ("fixed", "dynamic"):
            res = replay_day(recs, kind, o, TZ, prices, load, pv_fc, soc[kind], avg, replan_every_h,
                             month_peak_kw=m[kind]["peak_kw"],
                             block_negative=block_negative, peak_shave=peak_shave,
                             solar_hold=solar_hold)
            soc[kind] = res["soc_end"]
            if kind == "fixed":
                dyn = tariff("dynamic", o)
                for t, imp, exp in res["flows"]:
                    local = (t + timedelta(minutes=7)).astimezone(TZ)
                    mk = next(p.eur_mwh for p in prices if p.start <= t < p.end)
                    m["same"]["imp_night" if is_night(local, o) else "imp_day"] += imp
                    m["same"]["exp"] += exp
                    m["same"]["dyn_c"] += imp * dyn.buy(local, mk) - exp * dyn.sell(local, mk)
            acc = m[kind]
            for k in ("cost_c", "imp", "exp", "charged", "discharged", "grid_charged"):
                acc[k] += res[k]
            acc["peak_kw"] = max(acc["peak_kw"], res["peak_kw"])
        # no battery at all, for reference
        for r in recs:
            local = (r.start + timedelta(minutes=7)).astimezone(TZ)
            net = r.kwh["load"] - r.kwh["pv"]
            imp, exp = max(net, 0), max(-net, 0)
            mk = next(p.eur_mwh for p in prices if p.start <= r.start < p.end)
            for kind in ("fixed", "dynamic"):
                t = tariff(kind, o)
                m["nobat"][f"{kind}_c"] += imp * t.buy(local, mk) - exp * t.sell(local, mk, avg)
            m["nobat"]["peak_kw"] = max(m["nobat"]["peak_kw"], imp * 4)
        m["days"] += 1
        m["pv"] += sum(r.kwh["pv"] for r in recs)
        m["pv_fc"] += sum(v for h, v in fc.items() if h.astimezone(TZ).date() == d)
        m["load"] += sum(r.kwh["load"] for r in recs)
        m["market"].append(avg)
        if learn:
            archive = {h.isoformat(): v for h, v in fc.items()}
            shade.learn_records(recs, archive, TZ)
            load.learn_records(recs, TZ)
    return {"months": months, "shade": shade, "load": load, "options": o}


def month_eur(m: dict, kind: str, o: Options, battery: bool = True) -> float:
    fee = (o.fixed_yearly_fee_eur if kind == "fixed" else o.dynamic_yearly_fee_eur) / 12 * m["days"] / 30.4
    if battery:
        return m[kind]["cost_c"] / 100 + fee + capacity_month_eur(m[kind]["peak_kw"], o) * m["days"] / 30.4
    return m["nobat"][f"{kind}_c"] / 100 + fee + capacity_month_eur(m["nobat"]["peak_kw"], o) * m["days"] / 30.4


def table(res: dict) -> str:
    o = res["options"]
    rows = ["month    days  solar  fcast  market | FIXED  €   grid-chg  export  peak | DYNAMIC €  grid-chg  export"
            "  peak | dyn-fix | no battery fix / dyn",
            "-" * 140]
    tot = {"f": 0.0, "d": 0.0, "nf": 0.0, "nd": 0.0}
    for key, m in res["months"].items():
        f, d = month_eur(m, "fixed", o), month_eur(m, "dynamic", o)
        nf, nd = month_eur(m, "fixed", o, False), month_eur(m, "dynamic", o, False)
        tot["f"] += f
        tot["d"] += d
        tot["nf"] += nf
        tot["nd"] += nd
        mk = sum(m["market"]) / len(m["market"])
        rows.append(
            f"{key}  {m['days']:4d} {m['pv']:6.0f} {m['pv_fc']:6.0f} {mk:7.0f} | {f:7.0f} {m['fixed']['grid_charged']:9.0f}"
            f" {m['fixed']['exp']:7.0f} {m['fixed']['peak_kw']:5.2f} | {d:8.0f} {m['dynamic']['grid_charged']:9.0f}"
            f" {m['dynamic']['exp']:7.0f} {m['dynamic']['peak_kw']:5.2f} | {d - f:7.0f} | {nf:7.0f} / {nd:4.0f}")
    rows.append("-" * 140)
    rows.append(f"total: fixed €{tot['f']:.0f}, dynamic €{tot['d']:.0f}, dynamic - fixed €{tot['d'] - tot['f']:.0f}"
                f"   (without battery: fixed €{tot['nf']:.0f}, dynamic €{tot['nd']:.0f})")
    sh = res["shade"]
    rows.append("learned shade (true): " + "  ".join(
        f"{mo}/{h}h {sh.factor(mo, h):.2f} ({true_shade(mo, h):.1f})" for mo, h in ((1, 10), (1, 12), (1, 15), (7, 10), (7, 15))))
    rows.append("costs include energy, battery wear, contract fees and the capacity tariff. "
                "grid-chg = kWh bought from the grid for the battery.")
    return "\n".join(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--price-scale", type=float, default=1.0)
    ap.add_argument("--winter-mode", default="full_day", choices=["full_day", "peaks_only"])
    ap.add_argument("--block-negative", action="store_true", help="block export at negative sell prices")
    ap.add_argument("--peak-shave", action="store_true", help="the inverter has a peak-shaving switch")
    ap.add_argument("--solar-hold", action="store_true", help="the inverter has a feed-in priority switch")
    a = ap.parse_args()
    res = run(date.fromisoformat(a.start), a.days, Options(winter_mode=a.winter_mode), seed=a.seed,
              price_scale=a.price_scale, block_negative=a.block_negative, peak_shave=a.peak_shave,
              solar_hold=a.solar_hold)
    print(table(res))


if __name__ == "__main__":
    main()
