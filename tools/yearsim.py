"""A synthetic year for Battery Pilot: does it do the right things, and what does each contract cost?

The house: 6 kWp of panels near Antwerp, trees that shade the panels when the
sun is low behind them (true shade the app does not know and must learn; it
follows the sun's position, so it changes a little every day), about
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
import functools
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
from pilot.tariffs import capacity_month_eur, is_night, kinds, tariff  # noqa: E402

TZ = ZoneInfo("Europe/Brussels")
LAT, LON = 51.2, 4.4
KWP = 6.0
MARKET_BY_MONTH = [120, 110, 95, 80, 70, 65, 70, 75, 90, 105, 115, 125]   # EUR/MWh


def sun_position(t: datetime) -> tuple[float, float]:
    """(elevation, azimuth) of the sun in degrees at a UTC time (NOAA's approximation)."""
    t = t.astimezone(timezone.utc)
    hr = t.hour + t.minute / 60
    g = 2 * math.pi / 365 * (t.timetuple().tm_yday - 1 + (hr - 12) / 24)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    dec = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
           + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    ha = math.radians((hr * 60 + eqt + 4 * LON) / 4 - 180)
    lat = math.radians(LAT)
    cz = min(max(math.sin(lat) * math.sin(dec) + math.cos(lat) * math.cos(dec) * math.cos(ha), -1.0), 1.0)
    az = math.degrees(math.atan2(math.sin(ha), math.cos(ha) * math.sin(lat) - math.tan(dec) * math.cos(lat))) + 180
    return 90 - math.degrees(math.acos(cz)), az


def tree_line(azimuth: float) -> float:
    """How high the trees reach (degrees above the horizon) in a direction: tall trees to the
    south-east and south-west, a lower gap due south, open elsewhere."""
    if 90 <= azimuth < 160:
        return 20.0
    if 160 <= azimuth < 200:
        return 12.0
    if 200 <= azimuth < 270:
        return 18.0
    return 3.0


@functools.cache
def true_shade(hour: datetime) -> float:
    """What the trees let through in the hour that starts at `hour` (UTC): the sun behind them
    gives 30 % through bare branches (November to April), 15 % through leaves (May to October).
    It follows the sun, so it changes a little every day, fastest around March and September."""
    doy = hour.astimezone(TZ).timetuple().tm_yday
    leaves = min(max((doy - 105) / 25, 0.0), 1.0) * min(max((310 - doy) / 25, 0.0), 1.0)
    through = 0.3 - 0.15 * leaves
    num = den = 0.0
    for k in range(6):
        el, az = sun_position(hour + timedelta(minutes=5 + 10 * k))
        if el <= 0:
            continue
        clear = min(max((el - tree_line(az)) / 2 + 0.5, 0.0), 1.0)     # a 2° soft edge
        w = math.sin(math.radians(el))
        num += w * (clear + (1 - clear) * through)
        den += w
    return num / den if den else 1.0


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

    def actual(self, d: date, error: bool) -> dict[datetime, float]:
        """What the weather really gave (before shade). Without `error` the forecast was right about
        the day. With it the day is off (about a third either way), and on one day in four the
        afternoon differs from the morning."""
        fc = self.forecast(d)
        if not error:
            return fc
        rnd = random.Random(self.seed * 104729 + d.toordinal())
        cloud = self.cloud(d)
        am = min(max(cloud * math.exp(rnd.gauss(0, 0.35)), 0.03), 1.0)
        pm = min(max(cloud * math.exp(rnd.gauss(0, 0.35)), 0.03), 1.0) if rnd.random() < 0.25 else am
        return {h: v * (am if h.astimezone(TZ).hour < 13 else pm) / cloud for h, v in fc.items()}

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
            pv = fc.get(hour, 0.0) / 4 * true_shade(hour) * min(max(rnd.gauss(1, 0.15), 0.3), 1.4)
            load = self.load_kw(local) / 4 * min(max(rnd.gauss(1, 0.1), 0.6), 1.5)
            recs.append(QuarterRecord(t, {"import": 0.0, "export": 0.0, "load": load, "pv": pv},
                                      {"import": 1.0, "export": 1.0, "load": 1.0, "pv": 1.0}, None))
        return recs


# An example variable (monthly-indexed) formula in the shape of a Belgian tariff sheet: made-up
# numbers, not a real supplier's (`--variable`, and `demo_dashboard.py --variable`).
EXAMPLE_VARIABLE = dict(variable_day_mult=0.1, variable_day_add=1.5, variable_night_mult=0.09, variable_night_add=1.2,
                        variable_sell_mult=0.08, variable_sell_add=-1.0, variable_yearly_fee_eur=30)


def run(start: date, days: int, opts: Options | None = None, seed: int = 1, price_scale: float = 1.0,
        replan_every_h: int = 1, learn: bool = True, warmup_days: int = 7, block_negative: bool = False,
        peak_shave: bool = False, solar_hold: bool = False, forecast_error: bool = False,
        nowcast: bool = True) -> dict:
    """`warmup_days` before `start` are simulated for learning only (like a first install's history).
    `block_negative`: as with the *block negative export* switch on.
    `peak_shave`: the inverter has a peak-shaving switch. `solar_hold`: a feed-in priority switch.
    `forecast_error`: the solar forecast is wrong about the day (`World.actual`).
    `nowcast`: the plan corrects the forecast with the solar measured so far that day, as the app does."""
    o = opts or Options()
    w = World(seed, price_scale)
    shade, load = ShadeModel(), LoadProfile()
    kinds_ = kinds(o)                        # fixed, dynamic, and variable when its formula is filled in
    soc = {k: 50.0 for k in kinds_}
    months: dict[str, dict] = {}
    shade_err = shade_fc = 0.0              # how far the learned shade is from the trees, weighed by forecast
    for i in range(-warmup_days, days):
        d = start + timedelta(days=i)
        if i < 0:
            fc = w.forecast(d)
            recs = w.day_records(d, w.actual(d, forecast_error))
            load.learn_records(recs, TZ)
            continue
        fc = {**w.forecast(d), **w.forecast(d + timedelta(days=1))}
        prices = w.prices(d) + w.prices(d + timedelta(days=1))
        recs = w.day_records(d, w.actual(d, forecast_error))
        key = d.strftime("%Y-%m")
        m = months.setdefault(key, {"days": 0, "pv": 0.0, "pv_fc": 0.0, "load": 0.0, "market": [],
                                    **{k: {"cost_c": 0.0, "imp": 0.0, "exp": 0.0, "charged": 0.0, "discharged": 0.0,
                                           "grid_charged": 0.0, "peak_kw": 0.0} for k in kinds_},
                                    "nobat": {**{f"{k}_c": 0.0 for k in kinds_}, "peak_kw": 0.0},
                                    # what the meter would show on the fixed contract ("same usage" view)
                                    "same": {"imp_day": 0.0, "imp_night": 0.0, "exp": 0.0, "dyn_c": 0.0}})
        today = [p.eur_mwh for p in prices if p.start.astimezone(TZ).date() == d]
        avg = sum(today) / len(today)
        index = (sum(m["market"]) + avg) / (len(m["market"]) + 1)       # a variable contract: the month so far
        for h, v in fc.items():
            if h.astimezone(TZ).date() == d:
                shade_err += v * abs(shade.at(h, TZ) - true_shade(h))
                shade_fc += v
        pvf = PvForecast(fc, shade, TZ)
        pv_fc = pvf.kwh
        for kind in kinds_:
            res = replay_day(recs, kind, o, TZ, prices, load, pv_fc, soc[kind],
                             index if kind == "variable" else avg, replan_every_h, index=index,
                             month_peak_kw=m[kind]["peak_kw"],
                             block_negative=block_negative, peak_shave=peak_shave,
                             solar_hold=solar_hold,
                             on_replan=pvf.observe if nowcast else None)
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
            for kind in kinds_:
                t = tariff(kind, o, index)
                m["nobat"][f"{kind}_c"] += imp * t.buy(local, mk) - exp * t.sell(local, mk, index if kind == "variable" else avg)
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
    return {"months": months, "shade": shade, "load": load, "options": o,
            "shade_error_pct": 100 * shade_err / shade_fc if shade_fc else None}


def month_eur(m: dict, kind: str, o: Options, battery: bool = True) -> float:
    fee = tariff(kind, o).yearly_fee_eur / 12 * m["days"] / 30.4
    if battery:
        return m[kind]["cost_c"] / 100 + fee + capacity_month_eur(m[kind]["peak_kw"], o) * m["days"] / 30.4
    return m["nobat"][f"{kind}_c"] / 100 + fee + capacity_month_eur(m["nobat"]["peak_kw"], o) * m["days"] / 30.4


def table(res: dict) -> str:
    o = res["options"]
    rows = ["month    days  solar  fcast  market | FIXED  €   grid-chg  export  peak | DYNAMIC €  grid-chg  export"
            "  peak | dyn-fix | no battery fix / dyn",
            "-" * 140]
    tot = {"f": 0.0, "d": 0.0, "nf": 0.0, "nd": 0.0, "v": 0.0}
    for key, m in res["months"].items():
        f, d = month_eur(m, "fixed", o), month_eur(m, "dynamic", o)
        if "variable" in m:
            tot["v"] += month_eur(m, "variable", o)
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
                f"   (without battery: fixed €{tot['nf']:.0f}, dynamic €{tot['nd']:.0f})"
                + (f"; variable €{tot['v']:.0f}, variable - fixed €{tot['v'] - tot['f']:.0f}" if o.variable_set else ""))
    sh = res["shade"]
    year = next(iter(res["months"]))[:4]
    at = [datetime.fromisoformat(f"{year}-{md}T{h:02d}:00").replace(tzinfo=TZ).astimezone(timezone.utc)
          for md, h in (("01-15", 10), ("01-15", 12), ("03-01", 9), ("03-20", 9), ("07-15", 12), ("10-15", 16))]
    rows.append("learned shade (true): " + "  ".join(
        f"{t.astimezone(TZ):%d/%m %Hh} {sh.at(t, TZ):.2f} ({true_shade(t):.2f})" for t in at)
        + (f"; on the days replayed it was off by {res['shade_error_pct']:.1f} % of the forecast"
           if res["shade_error_pct"] is not None else ""))
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
    ap.add_argument("--forecast-error", action="store_true", help="the solar forecast is wrong about the day")
    ap.add_argument("--no-nowcast", action="store_true", help="do not correct the forecast during the day")
    ap.add_argument("--variable", action="store_true", help="also replay an example variable contract")
    a = ap.parse_args()
    o = Options(winter_mode=a.winter_mode, **(EXAMPLE_VARIABLE if a.variable else {}))
    res = run(date.fromisoformat(a.start), a.days, o, seed=a.seed,
              price_scale=a.price_scale, block_negative=a.block_negative, peak_shave=a.peak_shave,
              solar_hold=a.solar_hold, forecast_error=a.forecast_error, nowcast=not a.no_nowcast)
    print(table(res))


if __name__ == "__main__":
    main()
