"""The running app: reads HA every few seconds, plans, steers the Solis, compares contracts."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import statistics
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import prices as P
from .compare import KINDS, Comparison, day_records, replay_day
from .config import Options
from .controller import Decision, Readings, decide
from .discovery import ENTITIES, NEEDED_FOR, find_notify, resolve_all
from .helpers import CONTROL, CURTAIL, ensure_helpers
from .load import LoadProfile
from .meter import PeakTracker, QuarterMeter, QuarterRecord, history_to_hourly
from .migrate import migrate
from .planner import Plan, make_plan
from .solar import PvForecast, ShadeModel, archive_forecast, hourly_from_daily, parse_energy_forecast
from .solis import NEEDED as SOLIS_NEEDED
from .solis import Solis
from .tariffs import breakeven_eur_mwh, capacity_month_eur, is_night, tariff

log = logging.getLogger("battery_pilot")

TICK_S = 10
REFRESH_S = 300
SAVE_S = 300
PUBLISH_S = 30
NIGHTLY_AT = time(0, 20)
ALERTS_AT = time(9, 0)
RECORD_DAYS = 3


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class App:
    def __init__(self, opts: Options, ha, data_dir: str = "/data", config_dir: str | None = None,
                 clock=utcnow, version: str = "dev"):
        self.o = opts
        self.ha = ha
        self.data_dir = data_dir
        self.config_dir = config_dir
        self.clock = clock
        self.version = version
        self.tz = ZoneInfo("UTC")
        self.lat, self.lon = 50.85, 4.35
        self.ents: dict = {}
        self.ent_ids: dict[str, str] = {}
        self.notify: str | None = None
        self.problem: str | None = "starting"
        self.ready = False
        # learned / persistent state
        self.meter = QuarterMeter()
        self.peaks = PeakTracker()
        self.shade = ShadeModel()
        self.load = LoadProfile()
        self.comparison = Comparison()
        self.price_slots: list[P.PriceSlot] = []
        self.forecast_archive: dict[str, float] = {}
        self.records: list[QuarterRecord] = []
        self.daily_market: dict[str, float] = {}
        self.alerts: dict[str, str] = {}
        self.last_nightly = ""
        self.migrated = False
        # live state
        self.pv = PvForecast({}, self.shade, self.tz)
        self.plan: Plan | None = None
        self.solis: Solis | None = None
        self.readings: Readings | None = None
        self.decision = Decision("none", reason="starting")
        self.control_on = False
        self.curtail_on = False
        self.curtailed_by_us = False      # we set the export limit to 0 (persisted)
        self.peak_warned = ""
        self.over_since: datetime | None = None
        self.events: list[dict] = []
        self._last = {"refresh": None, "save": None, "publish": None}

    # ---------------------------------------------------------------- logging for the dashboard
    def event(self, text: str, level: int = logging.INFO):
        log.log(level, text)
        self.events.append({"t": self.clock().isoformat(), "text": text})
        del self.events[:-100]

    # ---------------------------------------------------------------- persistence
    @property
    def state_file(self) -> str:
        return os.path.join(self.data_dir, "state.json")

    def load_state(self) -> bool:
        try:
            with open(self.state_file, encoding="utf-8") as f:
                s = json.load(f)
        except (OSError, ValueError):
            return False
        self.peaks = PeakTracker.from_json(s.get("peaks"))
        self.shade = ShadeModel.from_json(s.get("shade"))
        self.load = LoadProfile.from_json(s.get("load"))
        self.comparison = Comparison.from_json(s.get("comparison"))
        self.price_slots = P.from_json(s.get("prices"))
        self.forecast_archive = dict(s.get("forecast_archive") or {})
        self.records = [QuarterRecord.from_json(r) for r in s.get("records") or []]
        self.daily_market = dict(s.get("daily_market") or {})
        self.alerts = dict(s.get("alerts") or {})
        self.last_nightly = s.get("last_nightly", "")
        self.migrated = bool(s.get("migrated"))
        self.curtailed_by_us = bool(s.get("curtailed_by_us"))
        if s.get("load_default_kw"):
            self.load.default_kw = s["load_default_kw"]
        self.pv = PvForecast({}, self.shade, self.tz)
        return True

    def save_state(self):
        os.makedirs(self.data_dir, exist_ok=True)
        s = {"version": self.version, "peaks": self.peaks.to_json(), "shade": self.shade.to_json(),
             "load": self.load.to_json(), "load_default_kw": self.load.default_kw,
             "comparison": self.comparison.to_json(), "prices": P.to_json(self.price_slots),
             "forecast_archive": self.forecast_archive, "records": [r.to_json() for r in self.records],
             "daily_market": self.daily_market, "alerts": self.alerts, "last_nightly": self.last_nightly,
             "migrated": self.migrated, "curtailed_by_us": self.curtailed_by_us}
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f)
        os.replace(tmp, self.state_file)

    # ---------------------------------------------------------------- setup
    async def setup(self) -> bool:
        cfg = await self.ha.config()
        self.tz = ZoneInfo(cfg.get("time_zone") or "UTC")
        self.lat = float(cfg.get("latitude") or self.lat)
        self.lon = float(cfg.get("longitude") or self.lon)
        fresh = not self.load_state()
        self.pv = PvForecast({}, self.shade, self.tz)
        states = await self.ha.states()

        self.event(f"Battery Pilot {self.version}: contract {self.o.contract}, battery "
                   f"{self.o.battery_capacity_kwh:g} kWh, import limit {self.o.import_limit_kw:g} kW")
        self.ents, missing = resolve_all(self.o, states)
        for key, f in self.ents.items():
            if f.entity_id:
                unit = f" [{f.unit}]" if ENTITIES[key][3] else ""
                log.info("  ok       %s: %s%s (%s)", key, f.entity_id, unit, f.how)
            else:
                tag = "MISSING" if ENTITIES[key][2] else "missing"
                extra = f" (needed for {NEEDED_FOR[key]})" if key in NEEDED_FOR else ""
                log.warning("  %-8s %s: %s%s", tag, key, f.how, extra)
        if missing:
            self.problem = ("Required entities are missing: " + ", ".join(missing) +
                            ". Set them on the app's Configuration tab (Show unused optional configuration "
                            "options), then restart the app.")
            self.event(self.problem, logging.ERROR)
            return False
        self.ent_ids = {k: f.entity_id for k, f in self.ents.items() if f.entity_id}
        self.notify, how = find_notify(self.o, await self.ha.services())
        log.info("  %-8s notify: %s (%s)", "ok" if self.notify else "check", self.notify or "-", how)
        if not self.ent_ids.get("price_sensor") and self.o.fixed_sell_mult != 0:
            self.event("No price sensor: dynamic prices, the contract comparison and the fixed injection "
                       "price (which follows the market) are unavailable. Without a price integration, set "
                       "fixed_sell_mult to 0 and fixed_sell_add to your injection rate.", logging.WARNING)
        try:
            for line in await ensure_helpers(self.ha, states):
                self.event(line)
        except Exception as e:  # noqa: BLE001
            self.event(f"Could not create the Battery Pilot helpers ({e}); control stays off.", logging.ERROR)
        self.problem = None
        self.solis = Solis(self.ha, {k: v for k, v in self.ent_ids.items() if k in SOLIS_NEEDED or k == "export_limit"})

        if not self.migrated:
            notes = await migrate(self.ha, states, self.config_dir, os.path.join(self.data_dir, "playbook_backup"),
                                  self.comparison, self.load, self.peaks, self.clock(), self.tz)
            for n in notes:
                self.event(n)
            self.migrated = True
        if fresh:
            await self.bootstrap()
        await self.refresh()
        self.save_state()
        self.ready = True
        return True

    async def bootstrap(self):
        """Learn load and shade from the recorder (usually the last 10 days) on the first start."""
        end = self.clock()
        start = end - timedelta(days=10)
        ids = [self.ent_ids.get(k) for k in ("house_load_power", "pv_power", "forecast_power_now")]
        try:
            hist = await self.ha.history([i for i in ids if i], start, end)
        except Exception as e:  # noqa: BLE001
            self.event(f"Could not read history to learn from ({e}); learning starts today.", logging.WARNING)
            return
        load_id, pv_id, fc_id = ids
        if load_id in hist:
            quarters = history_to_hourly(hist[load_id], self.ents["house_load_power"].scale, start, end, minutes=15)
            n = self.load.learn_quarters(quarters, self.tz)
            self.event(f"Learned house consumption from {n} hours of history ({self.load.daily_kwh()} kWh/day).")
        if pv_id in hist and fc_id in hist:
            pv = history_to_hourly(hist[pv_id], self.ents["pv_power"].scale, start, end)
            fc_unit = ((self.ents.get("forecast_power_now") and self.ents["forecast_power_now"].unit) or "W")
            fc = history_to_hourly(hist[fc_id], 1.0 if fc_unit == "kW" else 0.001, start, end)
            n = self.shade.learn_hourly(pv, fc, self.tz)
            self.event(f"Learned shade from {n} hours of history.")

    # ---------------------------------------------------------------- reading HA
    async def _states(self, keys) -> dict[str, dict | None]:
        ids = [(k, self.ent_ids[k]) for k in keys if k in self.ent_ids]
        res = await asyncio.gather(*(self.ha.state(eid) for _, eid in ids), return_exceptions=True)
        return {k: (r if isinstance(r, dict) else None) for (k, _), r in zip(ids, res)}

    @staticmethod
    def _num(st: dict | None) -> float | None:
        try:
            return float(st["state"]) if st else None
        except (TypeError, ValueError, KeyError):
            return None

    def _kw(self, key, sts) -> float | None:
        v = self._num(sts.get(key))
        return None if v is None else v * self.ents[key].scale

    # ---------------------------------------------------------------- the 10-second tick
    async def tick(self):
        now = self.clock()
        keys = ("grid_import_power", "grid_export_power", "house_load_power", "pv_power", "battery_soc",
                "battery_voltage")
        sts = await self._states(keys)
        helpers = await asyncio.gather(self.ha.state(CONTROL), self.ha.state(CURTAIL), return_exceptions=True)
        self.control_on = isinstance(helpers[0], dict) and helpers[0].get("state") == "on"
        self.curtail_on = isinstance(helpers[1], dict) and helpers[1].get("state") == "on"
        kw = {"import": self._kw("grid_import_power", sts), "export": self._kw("grid_export_power", sts),
              "load": self._kw("house_load_power", sts), "pv": self._kw("pv_power", sts)}
        soc = self._num(sts.get("battery_soc"))
        for rec in self.meter.add(now, kw, soc):
            self._quarter_closed(rec)

        local = now.astimezone(self.tz)
        if soc is None or kw["import"] is None or kw["load"] is None:
            self.readings = None
            self.decision = Decision("none", reason="battery SOC, grid import or house load unavailable")
            return
        voltage = self._num(sts.get("battery_voltage")) or self.o.battery_voltage
        projection = self.meter.projection_kw(now, kw["import"])
        self.readings = Readings(now, local, soc, kw["load"], kw["pv"] or 0.0, kw["import"], projection, voltage)
        step = self.plan.step_at(now) if self.plan else None
        if self.solis:
            self.decision = decide(step, self.readings, self.o, self.solis.mode, self.solis.slot_hour,
                                   self.solis.amps, self.control_on)
            await self._apply(self.decision, local)
            await self._curtail(local)
        await self._peak_warning(now, projection)

    async def _apply(self, d: Decision, local: datetime):
        try:
            if d.command == "charge":
                await self.solis.charge(local, d.amps)
            elif d.command == "discharge":
                await self.solis.discharge(local, d.amps)
            elif d.command == "current":
                await self.solis.set_current(local, d.amps)
            elif d.command == "self_use":
                await self.solis.self_use(local, d.reason)
        except Exception as e:  # noqa: BLE001
            self.event(f"Inverter command '{d.command}' failed: {e}", logging.ERROR)
            self.solis.mode = "unknown"

    async def _curtail(self, local: datetime):
        if "export_limit" not in self.ent_ids:
            return
        sell = self.current_prices()["sell"] if self.price_slots else None
        want = bool(self.control_on and self.curtail_on and sell is not None and sell < 0)
        # Only touch the export limit for this feature, and only undo what we did ourselves:
        # a zero-export installation keeps its own setting.
        if want == self.curtailed_by_us:
            return
        watts = 0 if want else int(self.o.export_limit_kw * 1000)
        try:
            await self.solis.set_export_limit(local, watts)
            self.curtailed_by_us = want
            self.save_state()
        except Exception as e:  # noqa: BLE001
            self.event(f"Setting the export limit failed: {e}", logging.ERROR)

    async def _peak_warning(self, now: datetime, projection: float):
        if projection > self.o.import_limit_kw + 0.3:
            self.over_since = self.over_since or now
            q = self.meter.current.isoformat() if self.meter.current else ""
            if now - self.over_since >= timedelta(minutes=2) and self.peak_warned != q:
                self.peak_warned = q
                r = self.readings
                await self.alert("Capacity peak warning",
                                 f"This quarter-hour is heading for {projection:.2f} kW (limit {self.o.import_limit_kw:g} kW;"
                                 f" battery {r.soc:.0f} %, house {r.load_kw:.2f} kW).", key=None, phone=True)
        else:
            self.over_since = None

    def _quarter_closed(self, rec: QuarterRecord):
        self.peaks.add(rec, self.tz)
        market = P.price_at(self.price_slots, rec.start)
        self.comparison.add_quarter(rec, market, self.o, self.tz)
        self.records.append(rec)
        cutoff = rec.start - timedelta(days=RECORD_DAYS)
        self.records = [r for r in self.records if r.start > cutoff]

    # ---------------------------------------------------------------- prices, forecast, plan (every 5 min)
    def market_avg_month(self, t: datetime) -> float | None:
        key = t.astimezone(self.tz).strftime("%Y-%m")
        vals = [v for d, v in self.daily_market.items() if d.startswith(key)]
        if vals:
            return statistics.fmean(vals)
        if self.price_slots:
            return statistics.fmean(p.eur_mwh for p in self.price_slots)
        return None

    def market_avg_30d(self) -> float | None:
        vals = [v for _, v in sorted(self.daily_market.items())[-30:]]
        return round(statistics.fmean(vals), 1) if vals else None

    async def refresh(self):
        now = self.clock()
        if "price_sensor" in self.ent_ids:
            try:
                new = P.parse_state(await self.ha.state(self.ent_ids["price_sensor"]))
                self.price_slots = P.merge(self.price_slots, new, now - timedelta(days=RECORD_DAYS))
            except Exception as e:  # noqa: BLE001
                self.event(f"Reading prices failed: {e}", logging.WARNING)
            self._update_daily_market(now)
        await self._refresh_forecast(now)
        self.replan(now)

    def _update_daily_market(self, now: datetime):
        by_day: dict[str, list[float]] = {}
        for p in self.price_slots:
            by_day.setdefault(p.start.astimezone(self.tz).date().isoformat(), []).append(p.eur_mwh)
        today = now.astimezone(self.tz).date().isoformat()
        for d, vals in by_day.items():
            if d <= today and len(vals) >= 20:
                self.daily_market[d] = round(statistics.fmean(vals), 2)
        for d in sorted(self.daily_market)[:-400]:
            del self.daily_market[d]

    async def _refresh_forecast(self, now: datetime):
        hourly: dict = {}
        try:
            hourly = parse_energy_forecast((await self.ha.ws({"type": "energy/solar_forecast"}))[0])
        except Exception as e:  # noqa: BLE001
            log.debug("energy/solar_forecast unavailable: %s", e)
        if not hourly:
            today = now.astimezone(self.tz).date()
            sts = await self._states(("forecast_today", "forecast_tomorrow"))
            for key, day in (("forecast_today", today), ("forecast_tomorrow", today + timedelta(days=1))):
                v = self._num(sts.get(key))
                if v is not None:
                    hourly.update(hourly_from_daily(day, v, self.lat, self.lon, self.tz))
        if hourly:
            self.forecast_archive = archive_forecast(self.forecast_archive, hourly, now)
        self.pv = PvForecast(hourly, self.shade, self.tz)

    def replan(self, now: datetime | None = None):
        now = now or self.clock()
        soc = self.readings.soc if self.readings else None
        if soc is None:
            st = self.meter.soc_at_start
            soc = st if st is not None else 50.0
        tar = tariff(self.o.contract, self.o)
        if self.o.contract == "dynamic" and not self.price_slots:
            self.plan = None
            self.problem = "Dynamic contract but no prices: check the price sensor."
            return
        if self.problem and self.problem.startswith("Dynamic contract"):
            self.problem = None
        self.plan = make_plan(now, self.tz, self.o, tar, self.price_slots, lambda a, b: self.load.kwh(a, b, self.tz),
                              self.pv.kwh, soc, market_avg=self.market_avg_month(now),
                              curtail_negative=self.curtail_on, forecast_until=self.pv.until,
                              peak_kw=lambda a, b: self.load.peak_kw(a, b, self.tz))

    def current_prices(self) -> dict:
        now = self.clock()
        local = now.astimezone(self.tz)
        market = P.price_at(self.price_slots, now)
        avg = self.market_avg_month(now)
        out = {"market": market}
        for kind in KINDS:
            t = tariff(kind, self.o)
            if kind == "dynamic" and market is None:
                out[kind] = None
                continue
            out[kind] = {"buy": round(t.buy(local, market), 2), "sell": round(t.sell(local, market, avg), 2)}
        cur = out.get(self.o.contract) or {}
        out["buy"], out["sell"] = cur.get("buy"), cur.get("sell")
        out["night"] = is_night(local, self.o)
        return out

    # ---------------------------------------------------------------- nightly learning and replay
    async def nightly(self):
        now = self.clock()
        local = now.astimezone(self.tz)
        today = local.date().isoformat()
        if self.last_nightly == today or local.time() < NIGHTLY_AT:
            return
        self.last_nightly = today
        y = local.date() - timedelta(days=1)
        start = datetime.combine(y, time(0), self.tz).astimezone(timezone.utc)
        end = datetime.combine(local.date(), time(0), self.tz).astimezone(timezone.utc)
        recs = day_records(self.records, start, end)
        if not recs:          # first day after install: nothing measured yet
            return
        n_shade = self.shade.learn_records(recs, self.forecast_archive, self.tz)
        n_load = self.load.learn_records(recs, self.tz)
        self.event(f"Learned from {y}: {n_load} hours of consumption, {n_shade} hours of solar vs forecast.")
        if self.price_slots or self.o.contract == "fixed":
            # The replay is CPU work: run it in a thread on copies, apply the results here.
            soc0 = {k: self.comparison.sim_soc.get(k) for k in KINDS}
            results = await asyncio.get_running_loop().run_in_executor(
                None, self._replay, list(recs), start, list(self.price_slots), dict(self.forecast_archive), soc0)
            self._apply_replay(y, results)
        self.comparison.prune()
        self.save_state()

    def _replay(self, recs: list[QuarterRecord], start: datetime, price_slots, forecast_archive, soc0: dict):
        archive = {datetime.fromisoformat(k): v for k, v in forecast_archive.items()}
        pv_fc = PvForecast(archive, self.shade, self.tz)
        avg = self.market_avg_month(start)
        results = {}
        for kind in KINDS:
            if kind == "dynamic" and not price_slots:
                continue
            s0 = soc0.get(kind)
            if s0 is None:
                s0 = recs[0].soc if recs and recs[0].soc is not None else 50.0
            res = replay_day(recs, kind, self.o, self.tz, price_slots, self.load, pv_fc.kwh, s0, avg)
            if res is None:
                return None
            results[kind] = res
        return results

    def _apply_replay(self, day: date, results: dict | None):
        if results is None:
            self.event(f"Replay of {day} skipped: too much data missing.")
            return
        key = day.strftime("%Y-%m")
        for kind, res in results.items():
            self.comparison.add_replay(key, kind, res)
            self.comparison.sim_soc[kind] = res["soc_end"]
        if len(results) == len(KINDS):
            self.comparison.month(key)["replay_days"] += 1
            f, d = results["fixed"]["cost_c"] / 100, results["dynamic"]["cost_c"] / 100
            self.event(f"Replayed {day}: fixed €{f:.2f}, dynamic €{d:.2f} (energy and battery wear).")

    # ---------------------------------------------------------------- alerts
    async def alert(self, title: str, message: str, key: str | None, phone: bool = True):
        if key:
            if key in self.alerts:
                return
            self.alerts[key] = self.clock().isoformat()
        self.event(f"{title}: {message}")
        try:
            await self.ha.call("persistent_notification", "create", {"title": title, "message": message})
            if phone and self.notify:
                domain, service = self.notify.split(".", 1)
                await self.ha.call(domain, service, {"title": title, "message": message})
        except Exception as e:  # noqa: BLE001
            self.event(f"Sending '{title}' failed: {e}", logging.WARNING)

    async def daily_alerts(self):
        now = self.clock()
        local = now.astimezone(self.tz)
        if local.time() < ALERTS_AT:
            return
        if local.day == 1:
            prev = (local.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
            if prev in self.comparison.months:
                r = self.comparison.report(prev, self.o)
                await self.alert(f"Contract check {prev}", self.verdict_text(r), key=f"monthly-{prev}")
        end = self.o.fixed_end_date
        if end and self.o.contract == "fixed":
            days = (end - local.date()).days
            if days in (42, 14, 3):
                s = self.comparison.summary(self.o, now, self.tz)
                diff = (s.get("with_strategy") or s.get("same_usage") or {}).get("dynamic_minus_fixed_eur")
                extra = ""
                if diff is not None:
                    extra = (f" Over the stored months dynamic would have been {'cheaper' if diff < 0 else 'dearer'}"
                             f" by about €{abs(diff):.0f}.")
                await self.alert(f"Fixed price ends in {days} days",
                                 f"Your fixed price ends on {end}. When the new rates arrive, enter them in the app's "
                                 f"settings for a new comparison.{extra}", key=f"renewal-{end}-{days}")
        avg30 = self.market_avg_30d()
        if avg30 is not None and self.o.contract == "fixed" and len(self.daily_market) >= 30:
            night_be = breakeven_eur_mwh(self.o.fixed_night_rate, self.o)
            day_be = breakeven_eur_mwh(self.o.fixed_day_rate, self.o)
            state = "below" if avg30 < night_be else ("above" if avg30 > day_be else "between")
            last = self.alerts.get("breakeven-state")
            if state != "between" and state != last:
                self.alerts["breakeven-state"] = state
                if state == "below":
                    await self.alert("Market below your fixed night rate",
                                     f"The 30-day market average is €{avg30:.0f}/MWh, under your €{night_be:.0f} "
                                     "break-even: every grid kWh is now cheaper on dynamic.", key=None)
                else:
                    await self.alert("Market above your fixed day rate",
                                     f"The 30-day market average is €{avg30:.0f}/MWh, above your €{day_be:.0f} "
                                     "break-even: every grid kWh is now dearer on dynamic.", key=None)
            elif state == "between":
                self.alerts["breakeven-state"] = state

    @staticmethod
    def verdict_text(r: dict) -> str:
        su = r["same_usage"]
        parts = [f"Same usage: fixed €{su['fixed_eur']:.0f}"]
        if su["dynamic_eur"] is not None:
            parts.append(f"dynamic €{su['dynamic_eur']:.0f}")
        ws = r.get("with_strategy")
        if ws:
            parts.append(f"with the best battery use for each ({r['replay_days']} days): fixed €{ws['fixed']['eur']:.0f},"
                         f" dynamic €{ws['dynamic']['eur']:.0f}")
        if r.get("market_avg_eur_mwh") is not None:
            parts.append(f"market average €{r['market_avg_eur_mwh']:.0f}/MWh")
        return " · ".join(parts) + "."

    # ---------------------------------------------------------------- sensors in HA
    async def publish(self):
        now = self.clock()
        local = now.astimezone(self.tz)
        sensors = []
        p = self.plan
        step = p.step_at(now) if p else None
        target = p.targets[0] if p and p.targets else None
        sensors.append(("sensor.battery_pilot_plan", step.action if step else "unknown", {
            "friendly_name": "Battery Pilot plan", "icon": "mdi:battery-sync",
            "mode": "active" if self.control_on else "shadow",
            "soc_target": round(step.soc_end, 1) if step else None,
            "reason": self.decision.reason, "winter": p.winter if p else None,
            "plan_cost_eur": p.to_json()["cost_eur"] if p else None,
            "next_hours": [{"start": s.start.astimezone(self.tz).strftime("%H:%M"), "action": s.action,
                            "soc": round(s.soc_end)} for s in (p.steps if p else [])
                           if s.start < now + timedelta(hours=24)][:96],
        }))
        if target:
            sensors.append(("sensor.battery_pilot_night_target", target["soc"], {
                "friendly_name": "Battery Pilot night target", "unit_of_measurement": "%", "icon": "mdi:battery-charging-high",
                "winter": target["winter"], "need_kwh": target["need_kwh"], "solar_kwh": target["solar_kwh"]}))
        for name, day in (("today", local.date()), ("tomorrow", local.date() + timedelta(days=1))):
            sensors.append((f"sensor.battery_pilot_solar_{name}", round(self.pv.day_total(day), 1), {
                "friendly_name": f"Battery Pilot solar {name} (shade-corrected)", "unit_of_measurement": "kWh",
                "device_class": "energy", "icon": "mdi:solar-power-variant",
                "forecast_kwh": round(self.pv.day_total(day, corrected=False), 1)}))
        if self.readings:
            sensors.append(("sensor.battery_pilot_quarter_projection", round(self.readings.projection_kw, 2), {
                "friendly_name": "Battery Pilot quarter-hour projection", "unit_of_measurement": "kW",
                "device_class": "power", "state_class": "measurement", "icon": "mdi:timer-sand"}))
        peak = self.peaks.month_peak(now, self.tz)
        sensors.append(("sensor.battery_pilot_month_peak", round(peak, 2), {
            "friendly_name": "Battery Pilot monthly peak", "unit_of_measurement": "kW", "device_class": "power",
            "icon": "mdi:chart-timeline-variant-shimmer", "billed_kw": max(peak, self.o.capacity_min_kw),
            "month_cost_eur": round(capacity_month_eur(peak, self.o), 2),
            "rolling_12_month_kw": self.peaks.rolling_average(self.o.capacity_min_kw)}))
        pr = self.current_prices()
        if pr.get("buy") is not None:
            sensors.append(("sensor.battery_pilot_buy_price", pr["buy"], {
                "friendly_name": "Battery Pilot buy price", "unit_of_measurement": "c/kWh", "icon": "mdi:cash-minus",
                "contract": self.o.contract, "fixed": pr["fixed"], "dynamic": pr["dynamic"], "market_eur_mwh": pr["market"]}))
            sensors.append(("sensor.battery_pilot_sell_price", pr["sell"], {
                "friendly_name": "Battery Pilot sell price", "unit_of_measurement": "c/kWh", "icon": "mdi:cash-plus",
                "contract": self.o.contract}))
        s = self.comparison.summary(self.o, now, self.tz)
        best = s.get("with_strategy") or s.get("same_usage")
        if best:
            sensors.append(("sensor.battery_pilot_contract_comparison", best["dynamic_minus_fixed_eur"], {
                "friendly_name": "Battery Pilot dynamic minus fixed", "unit_of_measurement": "EUR",
                "icon": "mdi:compare-horizontal", "basis": "with_strategy" if s.get("with_strategy") else "same_usage",
                "fixed_eur": best["fixed_eur"], "dynamic_eur": best["dynamic_eur"],
                "same_usage": s.get("same_usage"), "with_strategy": s.get("with_strategy")}))
        for eid, state, attrs in sensors:
            try:
                await self.ha.set_state(eid, state, attrs)
            except Exception as e:  # noqa: BLE001
                log.debug("publish %s failed: %s", eid, e)

    # ---------------------------------------------------------------- dashboard
    def status(self) -> dict:
        now = self.clock()
        r = self.readings
        return {
            "version": self.version, "now": now.isoformat(), "tz": str(self.tz), "problem": self.problem,
            "mode": "active" if self.control_on else "shadow", "contract": self.o.contract,
            "readings": None if r is None else {"soc": r.soc, "load_kw": round(r.load_kw, 3), "pv_kw": round(r.pv_kw, 3),
                                                "import_kw": round(r.import_kw, 3), "projection_kw": round(r.projection_kw, 2)},
            "decision": {"command": self.decision.command, "amps": self.decision.amps, "reason": self.decision.reason},
            "inverter": None if not self.solis else {"mode": self.solis.mode, "amps": self.solis.amps,
                                                      "log": list(reversed(self.solis.log[-20:]))},
            "plan": self.plan.to_json() if self.plan else None,
            "prices": self.current_prices(),
            "solar": {"today": round(self.pv.day_total(now.astimezone(self.tz).date()), 1),
                      "tomorrow": round(self.pv.day_total(now.astimezone(self.tz).date() + timedelta(days=1)), 1),
                      "shade": self.shade.to_json()["factors"], "shade_months": self.shade.month_summary()},
            "load": {"daily_kwh": self.load.daily_kwh(), "kw": self.load.to_json()["kw"]},
            "peaks": {"months": self.peaks.months, "limit_kw": self.o.import_limit_kw,
                      "rolling_kw": self.peaks.rolling_average(self.o.capacity_min_kw)},
            "comparison": self.comparison.summary(self.o, now, self.tz),
            "market_30d": self.market_avg_30d(),
            "breakeven": {"night": round(breakeven_eur_mwh(self.o.fixed_night_rate, self.o)),
                          "day": round(breakeven_eur_mwh(self.o.fixed_day_rate, self.o))},
            "events": list(reversed(self.events[-30:])),
        }

    # ---------------------------------------------------------------- main loop
    async def step(self):
        """One round of the main loop (called every TICK_S seconds)."""
        now = self.clock()
        await self.tick()
        if self._due("refresh", now, REFRESH_S):
            await self.refresh()
        await self.nightly()
        await self.daily_alerts()
        if self._due("publish", now, PUBLISH_S):
            await self.publish()
        if self._due("save", now, SAVE_S):
            self.save_state()

    def _due(self, name: str, now: datetime, every_s: int) -> bool:
        last = self._last.get(name)
        if last is None or (now - last).total_seconds() >= every_s:
            self._last[name] = now
            return True
        return False

    async def run(self, stop: asyncio.Event):
        while not stop.is_set():
            try:
                if not self.ready:
                    if not await self.ha.ping():
                        self.problem = "waiting for Home Assistant"
                        await _sleep(stop, 10)
                        continue
                    if not await self.setup():
                        await _sleep(stop, 300)
                        continue
                    self._last["refresh"] = self.clock()
                await self.step()
            except Exception as e:  # noqa: BLE001 - keep running, log it
                self._error(e)
            await _sleep(stop, TICK_S)
        if self.ready:
            await self.shutdown()

    def _error(self, e: Exception):
        """Log an error, but the same one at most every 5 minutes (e.g. while HA restarts)."""
        text = f"{type(e).__name__}: {e}"
        now = self.clock()
        last = self._last.get("error:" + text)
        if last is None or (now - last).total_seconds() >= 300:
            self._last["error:" + text] = now
            self.event(f"Error: {text}", logging.ERROR)
            log.debug("main loop", exc_info=e)

    async def shutdown(self):
        """App stopping: leave the inverter in self-use and save what was learned."""
        if self.solis and self.solis.mode in ("charge", "discharge"):
            try:
                await self.solis.self_use(self.clock().astimezone(self.tz), "Battery Pilot stopped")
            except Exception as e:  # noqa: BLE001
                log.warning("Could not return the inverter to self-use: %s", e)
        self.save_state()


async def _sleep(stop: asyncio.Event, seconds: float):
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except TimeoutError:
        pass
