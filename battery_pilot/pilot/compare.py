"""Fixed versus dynamic contract, from your own measured energy flows.

Two views, kept per month:

* **Same usage** - every quarter-hour, what you actually bought and sold is
  priced on both contracts. Simple and exact, but your battery behaved as it
  does on your current contract.
* **With the battery strategy of each contract** - every night, yesterday is
  replayed quarter by quarter with your measured house load and solar: the
  planner runs every hour as it would have on that contract (charging in
  cheap hours, exporting in dear ones, selling surplus solar, the winter night
  target), and the battery simulator applies its commands. Each contract keeps
  its own simulated battery from day to day. This answers "what would each
  contract have cost me, with the battery used the best way for it".
  Battery wear (wear cost x kWh discharged) is included, because a strategy
  that cycles more wears the battery more.

Contract fees and the capacity tariff are added at report time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Options
from .load import LoadProfile
from .meter import QuarterRecord
from .planner import make_plan
from .prices import PriceSlot, price_at
from .simulate import Battery
from .simulate import step as sim_step
from .tariffs import capacity_month_eur, is_night, tariff

KINDS = ("fixed", "dynamic")


def _month(t: datetime, tz: ZoneInfo) -> str:
    return t.astimezone(tz).strftime("%Y-%m")


def _blank():
    return {"imp_day": 0.0, "imp_night": 0.0, "exp": 0.0, "market_sum": 0.0, "market_n": 0,
            "dyn_c": 0.0, "quarters": 0, "replay": {k: {"cost_c": 0.0, "imp": 0.0, "exp": 0.0, "charged": 0.0,
                                                        "discharged": 0.0, "grid_charged": 0.0, "peak_kw": 0.0}
                       for k in KINDS},
            "replay_days": 0, "peak_kw": 0.0}


@dataclass
class Comparison:
    months: dict = field(default_factory=dict)            # "YYYY-MM" -> accumulators
    sim_soc: dict = field(default_factory=dict)           # contract -> simulated SOC carried over
    imported: dict = field(default_factory=dict)          # months imported from Battery Playbook

    def month(self, key: str) -> dict:
        m = self.months.setdefault(key, _blank())
        for k, v in _blank().items():                     # older state files: add new fields
            m.setdefault(k, v)
        return m

    # ---------------------------------------------------------------- same usage
    def add_quarter(self, rec: QuarterRecord, market: float | None, o: Options, tz: ZoneInfo) -> None:
        if rec.covered.get("import", 0) < 0.5:
            return
        m = self.month(_month(rec.start, tz))
        local = (rec.start + timedelta(minutes=7, seconds=30)).astimezone(tz)
        imp = rec.kwh.get("import", 0.0)
        exp = rec.kwh.get("export", 0.0) if rec.covered.get("export", 0) >= 0.5 else 0.0
        if is_night(local, o):
            m["imp_night"] += imp
        else:
            m["imp_day"] += imp
        m["exp"] += exp
        m["quarters"] += 1
        m["peak_kw"] = max(m["peak_kw"], imp * 4)
        if market is not None:
            m["market_sum"] += market
            m["market_n"] += 1
            dyn = tariff("dynamic", o)
            m["dyn_c"] += imp * dyn.buy(local, market) - exp * dyn.sell(local, market)

    # ---------------------------------------------------------------- replay
    def add_replay(self, key: str, kind: str, result: dict) -> None:
        r = self.month(key)["replay"][kind]
        for k in ("cost_c", "imp", "exp", "charged", "discharged", "grid_charged"):
            r[k] = r.get(k, 0.0) + result.get(k, 0.0)
        r["peak_kw"] = max(r["peak_kw"], result["peak_kw"])

    # ---------------------------------------------------------------- report
    def report(self, key: str, o: Options) -> dict:
        m = self.month(key)
        avg = m["market_sum"] / m["market_n"] if m["market_n"] else None
        fixed = tariff("fixed", o)
        fixed_sell = fixed.sell(None, None, avg if avg is not None else 0.0)
        fixed_energy = (m["imp_day"] * (o.fixed_day_rate + o.grid_fees_c_kwh)
                        + m["imp_night"] * (o.fixed_night_rate + o.grid_fees_c_kwh) - m["exp"] * fixed_sell) / 100
        cap = capacity_month_eur(m["peak_kw"], o)
        out = {
            "month": key,
            "market_avg_eur_mwh": round(avg, 1) if avg is not None else None,
            "bought_kwh": round(m["imp_day"] + m["imp_night"], 1),
            "sold_kwh": round(m["exp"], 1),
            "peak_kw": round(m["peak_kw"], 2),
            "same_usage": {
                "fixed_eur": round(fixed_energy + o.fixed_yearly_fee_eur / 12 + cap, 2),
                "dynamic_eur": round(m["dyn_c"] / 100 + o.dynamic_yearly_fee_eur / 12 + cap, 2)
                if m["market_n"] else None,
            },
            "replay_days": m["replay_days"],
            "with_strategy": None,
        }
        if m["replay_days"]:
            ws = {}
            for kind in KINDS:
                r = m["replay"][kind]
                fee = (o.fixed_yearly_fee_eur if kind == "fixed" else o.dynamic_yearly_fee_eur) / 12
                ws[kind] = {"eur": round(r["cost_c"] / 100 + fee + capacity_month_eur(r["peak_kw"], o), 2),
                            "bought_kwh": round(r["imp"], 1), "sold_kwh": round(r["exp"], 1),
                            "battery_in_kwh": round(r["charged"], 1), "battery_out_kwh": round(r["discharged"], 1),
                            "grid_charged_kwh": round(r.get("grid_charged", 0.0), 1),
                            "peak_kw": round(r["peak_kw"], 2)}
            out["with_strategy"] = ws
        if key in self.imported:
            out["imported_from_playbook"] = self.imported[key]
        return out

    def summary(self, o: Options, now: datetime, tz: ZoneInfo) -> dict:
        """Last 12 months (incl. the current one): totals and the difference dynamic - fixed."""
        keys = sorted(set(self.months) | set(self.imported))[-12:]
        reports = [self.report(k, o) for k in keys]
        same = [r for r in reports if r["same_usage"]["dynamic_eur"] is not None]
        strat = [r for r in reports if r["with_strategy"]]
        res = {"months": reports, "current": _month(now, tz)}
        if same:
            f = sum(r["same_usage"]["fixed_eur"] for r in same)
            d = sum(r["same_usage"]["dynamic_eur"] for r in same)
            res["same_usage"] = {"fixed_eur": round(f, 2), "dynamic_eur": round(d, 2),
                                 "dynamic_minus_fixed_eur": round(d - f, 2), "months": len(same)}
        if strat:
            f = sum(r["with_strategy"]["fixed"]["eur"] for r in strat)
            d = sum(r["with_strategy"]["dynamic"]["eur"] for r in strat)
            res["with_strategy"] = {"fixed_eur": round(f, 2), "dynamic_eur": round(d, 2),
                                    "dynamic_minus_fixed_eur": round(d - f, 2),
                                    "days": sum(r["replay_days"] for r in strat)}
        return res

    def prune(self, keep: int = 14) -> None:
        for k in sorted(self.months)[:-keep]:
            del self.months[k]

    def to_json(self):
        return {"months": self.months, "sim_soc": self.sim_soc, "imported": self.imported}

    @classmethod
    def from_json(cls, d):
        d = d or {}
        return cls(dict(d.get("months", {})), dict(d.get("sim_soc", {})), dict(d.get("imported", {})))


# ---------------------------------------------------------------- replay one day
def replay_day(records: list[QuarterRecord], kind: str, o: Options, tz: ZoneInfo, prices: list[PriceSlot],
               load: LoadProfile, pv_forecast, soc0: float, market_avg: float | None,
               replan_every_h: int = 1) -> dict | None:
    """Replay one day of measured quarters on a contract with its battery strategy.

    `pv_forecast(t0, t1)` is the (shade-corrected) forecast the app had; the plan uses
    forecasts, the simulation uses what was measured. Returns None when too much data is missing.
    """
    recs = sorted(records, key=lambda r: r.start)
    good = [r for r in recs if r.covered.get("load", 0) >= 0.5 and r.covered.get("pv", 0) >= 0.5]
    if len(recs) < 88 or len(good) < 0.75 * len(recs):
        return None
    b = Battery.from_options(o)
    tar = tariff(kind, o)
    soc = soc0
    out = {"cost_c": 0.0, "imp": 0.0, "exp": 0.0, "charged": 0.0, "discharged": 0.0, "peak_kw": 0.0,
           "grid_charged": 0.0, "soc_end": soc0}
    plan = None
    for i, r in enumerate(recs):
        if plan is None or (i % (4 * replan_every_h) == 0):
            plan = make_plan(r.start, tz, o, tar, prices, lambda a, z: load.kwh(a, z, tz), pv_forecast, soc,
                             market_avg=market_avg, curtail_negative=False, horizon_h=30,
                             peak_kw=lambda a, z: load.peak_kw(a, z, tz))
        st = plan.step_at(r.start + timedelta(minutes=7))
        action, target = (st.action, st.soc_end) if st else ("self_use", soc)
        if r.covered.get("load", 0) >= 0.5 and r.covered.get("pv", 0) >= 0.5:
            load_kwh, pv_kwh = r.kwh["load"], r.kwh["pv"]
        else:
            load_kwh = load.kwh(r.start, r.start + timedelta(minutes=15), tz)
            pv_kwh = pv_forecast(r.start, r.start + timedelta(minutes=15))
        left = (st.start + timedelta(hours=st.hours) - r.start).total_seconds() / 3600 if st else None
        res = sim_step(b, soc, action, target, load_kwh, pv_kwh, 0.25, slot_left_h=left)
        local = (r.start + timedelta(minutes=7, seconds=30)).astimezone(tz)
        market = price_at(prices, r.start)
        out["cost_c"] += res.import_kwh * tar.buy(local, market) - res.export_kwh * tar.sell(local, market, market_avg)
        # Battery wear counts too: a strategy that cycles more wears the battery more.
        out["cost_c"] += res.discharge_dc_kwh * o.wear_cost_c_kwh
        out["imp"] += res.import_kwh
        out["exp"] += res.export_kwh
        out["charged"] += res.charge_dc_kwh
        # AC energy into the battery beyond what solar surplus could give: bought from the grid
        out["grid_charged"] += max(res.charge_dc_kwh / b.eta_c - max(pv_kwh - load_kwh, 0.0), 0.0)
        out["discharged"] += res.discharge_dc_kwh
        out["peak_kw"] = max(out["peak_kw"], res.import_kwh * 4)
        soc = res.soc
    out["soc_end"] = soc
    return out


def day_records(records: list[QuarterRecord], day_start: datetime, day_end: datetime) -> list[QuarterRecord]:
    return [r for r in records if day_start <= r.start < day_end]


def utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)
