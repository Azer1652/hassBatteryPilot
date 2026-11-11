"""The running app: reads HA every few seconds, plans, steers the Solis, compares contracts."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import statistics
from datetime import date, datetime, time, timedelta, timezone
from time import monotonic
from zoneinfo import ZoneInfo

from . import car as C
from . import history as H
from . import prices as P
from . import trading
from .compare import KINDS, Comparison, day_records, replay_day
from .config import ConfigError, Options
from .controller import ACTIVE, FILL_AGAIN, Decision, Readings, decide
from .discovery import ENTITIES, NEEDED_FOR, find_notify, resolve_all
from .efficiency import EfficiencyMeter
from .helpers import CAR_FILL, CONTROL, CURTAIL, ensure_helpers
from .load import LoadProfile
from .meter import MAX_GAP, PeakTracker, QuarterMeter, QuarterRecord, history_to_hourly
from .migrate import migrate
from .planner import Plan, Step, make_plan, market_at, step_to_follow, sticky_action
from .simulate import Battery
from .solar import PvForecast, ShadeModel, archive_forecast, hourly_from_daily, parse_energy_forecast
from .solis import NEEDED as SOLIS_NEEDED
from .solis import OPTIONAL as SOLIS_OPTIONAL
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
BLIND_S = 60          # without SOC or grid import this long, a charge or discharge is stopped
HELPER_GRACE_S = 60   # a helper switch that cannot be read keeps its last state this long
FILL_MAX_H = 24       # the fill switch turns itself off this long after it was turned on
FROZEN_S = 1800       # a SOC that does not move this long while (dis)charging at a real current is stale
CMD_EVENT_S = 300     # the same failing inverter command is logged at most this often
TOU_SETTLE_S = 30     # after a command, give the time-of-use switch this long before reading it back
CMD_PROBLEM = "The inverter does not accept commands"
HEALTH_S = 600        # the main loop must have gone round this recently (it sleeps 300 s between setup tries)


def write_json(path: str, obj) -> None:
    """Write a file so that a power cut leaves either the old or the new version, never half of it."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class App:
    def __init__(self, opts: Options, ha, data_dir: str = "/data", config_dir: str | None = None,
                 clock=utcnow, version: str = "dev", options_path: str | None = None):
        self.o = opts
        self.options_path = options_path
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
        self._ha_wait: str | None = None       # last reason logged for not reaching HA
        # learned / persistent state
        self.meter = QuarterMeter()
        self.peaks = PeakTracker()
        self.shade = ShadeModel()
        self.efficiency = EfficiencyMeter()
        self.load = LoadProfile()
        self.comparison = Comparison()
        self._no_prices_told = False
        self.price_slots: list[P.PriceSlot] = []
        self.forecast_archive: dict[str, float] = {}
        self.records: list[QuarterRecord] = []
        self.daily_market: dict[str, float] = {}
        self.alerts: dict[str, str] = {}
        self.last_nightly = ""
        self.migrated = False
        self.day_logs: dict[str, dict] = {}   # date -> predictions made that day, until the day is closed
        self.history = H.HistoryStore(os.path.join(data_dir, "history"))
        # live state
        self.pv = PvForecast({}, self.shade, self.tz)
        self.plan: Plan | None = None
        self.solis: Solis | None = None
        self.readings: Readings | None = None
        self.decision = Decision("none", reason="starting")
        self.control_on = False
        self.curtail_on = False
        self.fill_on = False              # the "fill the battery for the car" helper
        self.fill_since = ""              # when it was turned on (UTC, ISO; persisted)
        self._fill_done = False           # a car session ended: the helper is turned off at this tick
        self.curtailed_by_us = False      # we set the export limit to 0 (persisted)
        self.export_limit_before: float | None = None   # the export limit we found before blocking (persisted)
        self.peak_warned = ""
        self.car = C.NoCar()              # where the car's state comes from (set in setup)
        self.car_state = C.IDLE
        self.car_months: dict[str, float] = {}     # kWh into the car per month (persisted)
        self._car_q = 0.0                 # car kWh in the quarter that is running
        self._car_t: datetime | None = None
        self._car_session: tuple[datetime, float] | None = None    # (start, kWh so far)
        self.trading: dict = {}           # what trading would earn in the coming hours, per contract (display)
        self.boost_seen: dict = {}        # the boost window last announced in the events (persisted)
        self.over_since: datetime | None = None
        self.events: list[dict] = []
        self._modes: dict[str, int] = {}  # inverter mode per tick in the current quarter
        self._blocked = False             # export was blocked by us at some tick in the current quarter
        self.seen_at: datetime | None = None    # last tick with SOC and grid import
        self.blind_reported = False
        self._helper_seen: dict[str, datetime] = {}
        self._soc_since: tuple[float, datetime] | None = None
        self._stuck_soc: float | None = None
        self._cmd_at: datetime | None = None
        self._cmd_failures = 0
        self._tou_mismatch = 0
        self._shave_mismatch = 0
        self._hold_mismatch = 0
        self._shave_soc = float(opts.battery_min_soc)
        self._inverter_saved: dict | None = None
        self._forecast_out = False
        self._refresh_task: asyncio.Task | None = None
        self.alive_at = monotonic()            # last round of the main loop (the Docker health check)
        self._last = {"refresh": None, "save": None, "publish": None}

    # ---------------------------------------------------------------- logging for the dashboard
    def event(self, text: str, level: int = logging.INFO):
        log.log(level, text)
        self.events.append({"t": self.clock().isoformat(), "text": text})
        del self.events[:-100]

    # ---------------------------------------------------------------- settings file
    def saved_options(self) -> dict:
        """The settings as saved on the Configuration tab (what the next start will use)."""
        try:
            with open(self.options_path or "", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            raise ConfigError(f"the settings cannot be read ({e}).") from None
        if not isinstance(raw, dict):
            raise ConfigError("the settings file does not hold settings.")
        return raw

    async def save_options(self, merged: dict, changed: int):
        """Save imported settings through the Supervisor. The running settings stay as they are
        until the app is restarted."""
        await self.ha.supervisor("POST", "/addons/self/options", {"options": merged})
        self.event(f"Settings imported ({changed} changed). Restart the app to use them.")

    # ---------------------------------------------------------------- persistence
    @property
    def state_file(self) -> str:
        return os.path.join(self.data_dir, "state.json")

    def load_state(self) -> bool:
        """What was learned and stored. A file that cannot be used is kept as state.json.bad and
        Battery Pilot starts fresh, rather than failing to start."""
        try:
            with open(self.state_file, encoding="utf-8") as f:
                s = json.load(f)
            if not isinstance(s, dict):
                raise ValueError("not a JSON object")
            loaded = {
                "peaks": PeakTracker.from_json(s.get("peaks")), "shade": ShadeModel.from_json(s.get("shade")),
                "efficiency": EfficiencyMeter.from_json(s.get("efficiency")),
                "car_months": {str(k): float(v) for k, v in (s.get("car_months") or {}).items()},
                "load": LoadProfile.from_json(s.get("load")), "comparison": Comparison.from_json(s.get("comparison")),
                "price_slots": P.from_json(s.get("prices")),
                "forecast_archive": dict(s.get("forecast_archive") or {}),
                "records": [QuarterRecord.from_json(r) for r in s.get("records") or []],
                "daily_market": dict(s.get("daily_market") or {}), "alerts": dict(s.get("alerts") or {}),
                "last_nightly": str(s.get("last_nightly") or ""), "migrated": bool(s.get("migrated")),
                "curtailed_by_us": bool(s.get("curtailed_by_us")), "fill_since": str(s.get("fill_since") or ""),
                "export_limit_before": s.get("export_limit_before"),
                "day_logs": dict(s.get("day_logs") or {}), "boost_seen": dict(s.get("boost_seen") or {}),
            }
            if s.get("load_default_kw"):
                loaded["load"].default_kw = float(s["load_default_kw"])
        except FileNotFoundError:
            return False
        except Exception as e:  # noqa: BLE001 - any damage: keep a copy, start fresh
            try:
                os.replace(self.state_file, self.state_file + ".bad")
            except OSError:
                pass
            self.event(f"The saved state could not be read ({type(e).__name__}: {e}); starting fresh. "
                       "A copy is kept as state.json.bad.", logging.WARNING)
            return False
        for k, v in loaded.items():
            setattr(self, k, v)
        if self.daily_market and not s.get("whole_day_averages") and any(p.minutes <= 15 for p in self.price_slots):
            # Saved before 2.4.1 with quarter-hour prices: each day's average was overwritten with
            # that of its evening. Forget them; the days still in the price archive are rebuilt.
            self.daily_market = {}
            self.event("The stored daily market averages were taken from parts of days (quarter-hour prices); "
                       "they are rebuilt from the prices of the last days.")
        self.pv = PvForecast({}, self.shade, self.tz)
        return True

    def save_state(self):
        os.makedirs(self.data_dir, exist_ok=True)
        s = {"version": self.version, "peaks": self.peaks.to_json(), "shade": self.shade.to_json(),
             "load": self.load.to_json(), "load_default_kw": self.load.default_kw,
             "comparison": self.comparison.to_json(), "prices": P.to_json(self.price_slots),
             "forecast_archive": self.forecast_archive, "records": [r.to_json() for r in self.records],
             "daily_market": self.daily_market, "alerts": self.alerts, "last_nightly": self.last_nightly,
             "migrated": self.migrated, "curtailed_by_us": self.curtailed_by_us,
             "export_limit_before": self.export_limit_before, "day_logs": self.day_logs,
             "boost_seen": self.boost_seen, "whole_day_averages": True,
             "efficiency": self.efficiency.to_json(), "car_months": self.car_months,
             "fill_since": self.fill_since}
        write_json(self.state_file, s)

    # What Battery Pilot left on the inverter, in a small file written at every change: after a
    # crash or power cut the next start can clean up (the Solis repeats its time-of-use slot daily).
    @property
    def inverter_file(self) -> str:
        return os.path.join(self.data_dir, "inverter.json")

    def _save_inverter(self):
        s = {"mode": self.solis.mode if self.solis else "unknown", "curtailed_by_us": self.curtailed_by_us,
             "export_limit_before": self.export_limit_before}
        if s == self._inverter_saved:
            return
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            write_json(self.inverter_file, s)
            self._inverter_saved = s
        except OSError as e:
            log.warning("Could not save the inverter state: %s", e)

    def _load_inverter(self) -> dict:
        try:
            with open(self.inverter_file, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

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
            elif ENTITIES[key][1] or key in self.o.entities:     # (never guessed and not set: nothing to report)
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
        self.car = C.make_source(self.o, self.ent_ids)
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
        self.solis = Solis(self.ha, {k: v for k, v in self.ent_ids.items() if k in SOLIS_NEEDED + SOLIS_OPTIONAL})
        if "peak_shaving_limit" in self.ent_ids:
            unit = ((states.get(self.ent_ids["peak_shaving_limit"]) or {}).get("attributes") or {}) \
                .get("unit_of_measurement")
            self.solis.shave_limit_per_kw = 1.0 if unit == "kW" else 1000.0
        # The level the inverter keeps in the battery while peak shaving: the battery minimum, so it
        # never refills the battery from the grid itself (or the lowest value the entity accepts).
        self._shave_soc = float(self.o.battery_min_soc)
        if "peak_shaving_soc" in self.ent_ids:
            low = ((states.get(self.ent_ids["peak_shaving_soc"]) or {}).get("attributes") or {}).get("min")
            if isinstance(low, (int, float)):
                self._shave_soc = max(self._shave_soc, float(low))
        if self.o.peak_buffer_pct > 0 and not self.can_peak_shave:
            self.event("The peak buffer needs the inverter's peak-shaving switch, which was not found: "
                       "it is off.", logging.WARNING)
            self.o.peak_buffer_pct = 0
        saved = self._load_inverter()
        if "curtailed_by_us" in saved:
            self.curtailed_by_us = bool(saved["curtailed_by_us"])
            self.export_limit_before = saved.get("export_limit_before")
        if saved.get("mode") in ACTIVE:
            # Stopped while the inverter was charging, discharging or peak shaving (crash, power cut):
            # the Solis repeats its time-of-use slot every day and stays in peak shaving, so it goes
            # back to self-use now.
            self.event("Battery Pilot had stopped while the inverter was charging, discharging or peak "
                       "shaving; it is set back to self-use.", logging.WARNING)
            await self._apply(Decision("self_use", reason="left active when Battery Pilot stopped"),
                              self.clock().astimezone(self.tz))
        soc = self._num(await self.ha.state(self.ent_ids["battery_soc"]))
        if soc is not None and self.meter.soc_at_start is None:
            self.meter.soc_at_start = soc            # the first plan starts from the real SOC

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
                "battery_voltage", "tou_switch", "peak_shaving_switch", "feed_in_priority_switch",
                "car_charger_power")
        sts = await self._states(keys)
        helpers = await asyncio.gather(self.ha.state(CONTROL), self.ha.state(CURTAIL), self.ha.state(CAR_FILL),
                                       return_exceptions=True)
        self.control_on = self._helper("control", helpers[0], self.control_on, now)
        self.curtail_on = self._helper("curtail", helpers[1], self.curtail_on, now)
        self.fill_on = self._helper("fill", helpers[2], self.fill_on, now)
        kw = {"import": self._kw("grid_import_power", sts), "export": self._kw("grid_export_power", sts),
              "load": self._kw("house_load_power", sts), "pv": self._kw("pv_power", sts)}
        soc = self._num(sts.get("battery_soc"))
        self._blocked = self._blocked or self.curtailed_by_us
        self._car(now, kw["load"], self._kw("car_charger_power", sts) if "car_charger_power" in self.ent_ids else None)
        await self._fill(now)
        for rec in self.meter.add(now, kw, soc):
            self._quarter_closed(rec)

        local = now.astimezone(self.tz)
        voltage = self._num(sts.get("battery_voltage")) or self.o.battery_voltage
        if kw["load"] is None and kw["import"] is not None:
            kw["load"] = self._estimate_load(kw, voltage)     # a flaky load sensor must not stop a charge
        if soc is None or kw["import"] is None or self._frozen(now, soc):
            await self._blind(now, local, "battery SOC or grid import unavailable" if soc is None or kw["import"] is None
                              else "the battery SOC does not change while (dis)charging")
            return
        self.seen_at, self.blind_reported = now, False
        projection = self.meter.projection_kw(now, kw["import"])
        self.readings = Readings(now, local, soc, kw["load"], kw["pv"] or 0.0, kw["import"], projection, voltage)
        step = self.plan.step_at(now) if self.plan else None
        if self.solis:
            self._reconcile(now, (sts.get("tou_switch") or {}).get("state"),
                            (sts.get("peak_shaving_switch") or {}).get("state"),
                            (sts.get("feed_in_priority_switch") or {}).get("state"))
            # (a charge or discharge is not stopped for the last minutes before a slot boundary)
            step = step_to_follow(self.plan, now, Battery.from_options(self.o), self.solis.mode)
            self.decision = decide(step, self.readings, self.o, self.solis.mode, self.solis.slot_hour,
                                   self.solis.amps, self.control_on,
                                   (now - self._cmd_at).total_seconds() if self._cmd_at else 1e9,
                                   car_kw=self.car_state.power_kw if self.car_state.charging else 0.0,
                                   can_shave=self.can_peak_shave, fill=self.fill_on)
            await self._apply(self.decision, local)
            await self._curtail(local)
        mode = self.solis.mode if self.control_on and self.solis else "shadow"
        self._modes[mode] = self._modes.get(mode, 0) + 1
        await self._peak_warning(now, projection, step)

    def _car(self, now: datetime, load_kw: float | None, charger_kw: float | None):
        """Is the car charging? Its energy is kept per quarter (so it is not learned as house
        consumption) and per month, and a session is reported when it starts and ends."""
        was = self.car_state
        if was.charging and self._car_t is not None and timedelta(0) < now - self._car_t <= MAX_GAP:
            kwh = was.power_kw * (now - self._car_t).total_seconds() / 3600
            self._car_q += kwh
            if self._car_session:
                self._car_session = (self._car_session[0], self._car_session[1] + kwh)
        self._car_t = now
        self.car_state = self.car.update(now, load_kw, charger_kw)
        if self.car_state.charging and not was.charging:
            self._car_session = (now, 0.0)
            what = ("peak shaving while it lasts" if self.can_peak_shave and self.control_on
                    else "measured only" if self.can_peak_shave
                    else "measured only (the inverter's peak-shaving switch was not found)")
            self.event(f"Car charging detected (about {self.car_state.power_kw:.1f} kW): {what}.")
        elif was.charging and not self.car_state.charging and self._car_session:
            self.event(f"Car charging ended: {self._car_session[1]:.1f} kWh.")
            self._car_session = None
            self._fill_done = True

    async def _fill(self, now: datetime):
        """The "fill the battery for the car" switch: note when it went on, and turn it off when the
        car has charged or after FILL_MAX_H (a forgotten switch would keep buying grid power)."""
        done, self._fill_done = self._fill_done, False
        if not self.fill_on:
            if self.fill_since:
                self.fill_since = ""
                self.event("Fill the battery for the car: off; the plan is followed again.")
            return
        if not self.fill_since:
            self.fill_since = now.isoformat()
            self.event("Fill the battery for the car: on. The battery is charged to 100 % under the import "
                       "limit and kept full" + ("." if self.control_on else
                                                " once Battery Pilot control is on."))
        old = now - datetime.fromisoformat(self.fill_since) >= timedelta(hours=FILL_MAX_H)
        if done or old:
            try:
                await self.set_fill(False)
            except Exception as e:  # noqa: BLE001 - tried again at the next tick or session end
                self._fill_done = done
                log.warning("Could not turn the fill switch off: %s", e)
                return
            self.fill_since = ""
            self.event("Fill the battery for the car: turned off, " +
                       ("the car has charged." if done else f"{FILL_MAX_H} hours have passed."))

    async def set_fill(self, on: bool):
        """Turn the fill helper on or off (the page's button, and the automatic end)."""
        await self.ha.call("input_boolean", "turn_on" if on else "turn_off", {"entity_id": CAR_FILL})
        self.fill_on = on
        self._helper_seen["fill"] = self.clock()

    def _helper(self, name: str, st, last: bool, now: datetime) -> bool:
        """A helper switch's state; a read that fails keeps the last known state for a minute."""
        if isinstance(st, dict) and st.get("state") in ("on", "off"):
            self._helper_seen[name] = now
            return st["state"] == "on"
        seen = self._helper_seen.get(name)
        if seen is not None and (now - seen).total_seconds() < HELPER_GRACE_S:
            return last
        return False

    def _estimate_load(self, kw: dict, voltage: float) -> float:
        """House load from grid, solar and the charge current we set, when the load sensor is out."""
        charge_kw = self.solis.amps * voltage / 1000 if self.solis and self.solis.mode == "charge" else 0.0
        return max(kw["import"] - (kw["export"] or 0.0) + (kw["pv"] or 0.0) - charge_kw, 0.0)

    def _frozen(self, now: datetime, soc: float) -> bool:
        """A SOC that stays the same while the battery is charged or discharged at a real current
        is a stale reading; it counts as stale until it changes."""
        if self._stuck_soc is not None:
            if soc == self._stuck_soc:
                return True
            self._stuck_soc = None
        s = self.solis
        moving = (s is not None and s.amps >= 15 and
                  ((s.mode == "charge" and soc < 99) or (s.mode == "discharge" and soc > self.o.battery_min_soc + 1)))
        if not moving or self._soc_since is None or soc != self._soc_since[0]:
            self._soc_since = (soc, now)
            return False
        if (now - self._soc_since[1]).total_seconds() >= FROZEN_S:
            self._stuck_soc, self._soc_since = soc, None
            return True
        return False

    async def _blind(self, now: datetime, local: datetime, why: str):
        """No usable SOC or grid import: after BLIND_S, a charge or discharge is stopped."""
        self.readings = None
        self.decision = Decision("none", reason=why)
        gone = (now - self.seen_at).total_seconds() if self.seen_at else BLIND_S
        if self.solis and self.solis.mode in ACTIVE and gone >= BLIND_S:
            if not self.blind_reported:
                self.blind_reported = True
                self.event(f"No usable measurements ({why}): the inverter goes back to self-use until they "
                           "return.", logging.WARNING)
            self.decision = Decision("self_use", reason="measurements unavailable")
            await self._apply(self.decision, local)

    def _reconcile(self, now: datetime, tou: str | None, shave: str | None = None, hold: str | None = None):
        """Read the time-of-use and peak-shaving switches back: if something else changed one, the
        next command corrects it."""
        s = self.solis
        settling = self._cmd_at is not None and (now - self._cmd_at).total_seconds() < TOU_SETTLE_S
        if not self.control_on or tou not in ("on", "off") or settling:
            self._tou_mismatch = self._shave_mismatch = self._hold_mismatch = 0
            return
        wrong = (tou == "on" and s.mode in ("self_use", "peak_shave", "solar_hold")) \
            or (tou == "off" and s.mode in ("charge", "discharge"))
        self._tou_mismatch = self._tou_mismatch + 1 if wrong else 0
        if self._tou_mismatch >= 2:
            self._tou_mismatch = 0
            s.mode = "failed" if tou == "on" else "self_use"
            self.event(f"The inverter's time-of-use switch is {tou}, not what Battery Pilot set; correcting it.")
        wrong = (hold == "on" and s.mode in ("self_use", "charge", "discharge", "peak_shave")) \
            or (hold == "off" and s.mode == "solar_hold")
        self._hold_mismatch = self._hold_mismatch + 1 if wrong else 0
        if self._hold_mismatch >= 2:
            self._hold_mismatch = 0
            s.mode, s.hold = ("failed", None) if hold == "on" else ("self_use", False)
            self.event(f"The inverter's feed-in priority switch is {hold}, not what Battery Pilot set; correcting it.")
        wrong = (shave == "on" and s.mode in ("self_use", "charge", "discharge", "solar_hold")) \
            or (shave == "off" and s.mode == "peak_shave")
        self._shave_mismatch = self._shave_mismatch + 1 if wrong else 0
        if self._shave_mismatch >= 2:
            self._shave_mismatch = 0
            # on: the next command switches it off again; off: the inverter is in self-use
            s.mode, s.shave = ("failed", None) if shave == "on" else ("self_use", False)
            self.event(f"The inverter's peak-shaving switch is {shave}, not what Battery Pilot set; correcting it.")

    async def _apply(self, d: Decision, local: datetime):
        if d.command not in ("charge", "discharge", "current", "self_use", "peak_shave", "solar_hold"):
            return
        self._cmd_at = self.clock()
        try:
            if d.command == "charge":
                await self.solis.charge(local, d.amps)
            elif d.command == "discharge":
                await self.solis.discharge(local, d.amps)
            elif d.command == "current":
                await self.solis.set_current(local, d.amps)
            elif d.command == "solar_hold":
                await self.solis.solar_hold(local)
            elif d.command == "peak_shave":
                await self.solis.peak_shave(local, self.o.import_limit_kw, self._shave_soc)
            else:
                await self.solis.self_use(local, d.reason)
            self._cmd_failures = 0
            if self.problem and self.problem.startswith(CMD_PROBLEM):
                self.problem = None
        except Exception as e:  # noqa: BLE001
            self.solis.mode = "failed"        # its state is not known: the next tick sends the command again
            self.solis.shave = self.solis.shave_set = self.solis.hold = None
            self._cmd_failures += 1
            now, key = self.clock(), "cmdfail:" + d.command
            last = self._last.get(key)
            if last is None or (now - last).total_seconds() >= CMD_EVENT_S:
                self._last[key] = now
                self.event(f"Inverter command '{d.command}' failed: {e}", logging.ERROR)
            if self._cmd_failures >= 3:
                self.problem = f"{CMD_PROBLEM}: {e}"
        self._save_inverter()

    @property
    def can_peak_shave(self) -> bool:
        """The inverter's peak-shaving switch was found: the plan may leave the house on the grid."""
        return "peak_shaving_switch" in self.ent_ids

    @property
    def can_solar_hold(self) -> bool:
        """The inverter's feed-in priority switch was found: the plan may put solar charging off."""
        return "feed_in_priority_switch" in self.ent_ids

    @property
    def blocks_negative_export(self) -> bool:
        """The block switch is on and there is an export limit to set: the plan and the
        nightly replay then count on no export while the sell price is negative."""
        return self.curtail_on and "export_limit" in self.ent_ids

    async def _curtail(self, local: datetime):
        if "export_limit" not in self.ent_ids:
            return
        sell = self.current_prices()["sell"] if self.price_slots else None
        want = bool(self.control_on and self.curtail_on and sell is not None and sell < 0)
        # Only touch the export limit for this feature, and only undo what we did ourselves:
        # a zero-export installation keeps its own setting.
        if want == self.curtailed_by_us:
            return
        normal = self.o.export_limit_kw * 1000
        if want:
            try:
                before = self._num(await self.ha.state(self.ent_ids["export_limit"]))
            except Exception:  # noqa: BLE001
                before = None
            if before is not None and before <= 0:
                return                    # a zero-export installation: nothing to block, and not ours to undo
            self.export_limit_before, watts = before, 0
        else:
            before = self.export_limit_before
            watts = int(min(before, normal)) if before is not None else int(normal)
        try:
            await self.solis.set_export_limit(local, watts)
            self.curtailed_by_us = want
            self.save_state()
            self._save_inverter()
        except Exception as e:  # noqa: BLE001
            self.event(f"Setting the export limit failed: {e}", logging.ERROR)

    async def _peak_warning(self, now: datetime, projection: float, step: Step | None = None):
        """`step`: the step the controller follows now (default: the plan's step at `now`)."""
        step = step or (self.plan.step_at(now) if self.plan else None)
        limit = step.limit_kw if step and step.limit_kw is not None else self.o.import_limit_kw   # a planned boost
        if projection > limit + 0.3:
            self.over_since = self.over_since or now
            q = self.meter.current.isoformat() if self.meter.current else ""
            if now - self.over_since >= timedelta(minutes=2) and self.peak_warned != q:
                self.peak_warned = q
                r = self.readings
                await self.alert("Capacity peak warning",
                                 f"This quarter-hour is heading for {projection:.2f} kW (limit {limit:g} kW;"
                                 f" battery {r.soc:.0f} %, house {r.load_kw:.2f} kW).", key=None, phone=True)
        else:
            self.over_since = None

    def _quarter_closed(self, rec: QuarterRecord):
        if self._modes:
            rec.mode = max(self._modes, key=self._modes.get)
            self._modes = {}
        # Export was blocked in this quarter: the inverter held the panels back, so its solar
        # is not what they could give (not learned as shade, not used as such in the replay).
        rec.blocked, self._blocked = self._blocked, False       # (the next tick sets it again while it lasts)
        if self._car_q > 0:
            rec.car, self._car_q = min(self._car_q, rec.kwh.get("load", 0.0)), 0.0
            key = rec.start.astimezone(self.tz).strftime("%Y-%m")
            self.car_months[key] = round(self.car_months.get(key, 0.0) + rec.car, 3)
            for old in sorted(self.car_months)[:-24]:
                del self.car_months[old]
        self.peaks.add(rec, self.tz)
        market, known = market_at(self.price_slots, rec.start) if self.price_slots else (None, False)
        self.comparison.add_quarter(rec, market, self.o, self.tz, estimated=market is not None and not known)
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
                eid = self.ent_ids["price_sensor"]
                new = P.parse_state(await self.ha.state(eid), self.tz)
                if not new and P.ENTSOE_CURRENT in eid:      # ENTSO-e: the list is on its average sensor
                    new = P.parse_state(await self.ha.state(eid.replace(P.ENTSOE_CURRENT, P.ENTSOE_LIST)), self.tz)
                if new:
                    self._no_prices_told = False
                elif not self._no_prices_told:
                    self._no_prices_told = True
                    self.event(f"{eid} has no price list in its attributes (prices, prices_today/prices_tomorrow "
                               "or raw_today/raw_tomorrow): no dynamic prices and no contract comparison. Set "
                               "price_sensor to the sensor that has the list; for ENTSO-e that is "
                               "'Average electricity price'.", logging.WARNING)
                self.price_slots = P.merge(self.price_slots, new, now - timedelta(days=RECORD_DAYS))
            except Exception as e:  # noqa: BLE001
                self.event(f"Reading prices failed: {e}", logging.WARNING)
            self._update_daily_market(now)
        await self._refresh_forecast(now)
        self.replan(now)

    def _update_daily_market(self, now: datetime):
        """The average market price of each day, from days whose prices are complete. A day that is
        being dropped from the price archive (or has a gap) keeps the average it had: what is left of
        it is its evening, which is dearer than the day."""
        by_day: dict[date, list[tuple[int, float]]] = {}
        for p in self.price_slots:
            by_day.setdefault(p.start.astimezone(self.tz).date(), []).append((p.minutes, p.eur_mwh))
        today = now.astimezone(self.tz).date()
        for d, vals in by_day.items():
            start, end = H.day_bounds(d, self.tz)                   # 23 or 25 hours on clock-change days
            minutes = sum(m for m, _ in vals)
            if d <= today and minutes >= H.minutes(end, start):
                self.daily_market[d.isoformat()] = round(sum(m * v for m, v in vals) / minutes, 2)
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
            self._forecast_out = False
            self.forecast_archive = archive_forecast(self.forecast_archive, hourly, now)
        else:
            # The forecast is unavailable (service down, rate limit): keep planning with the last one
            # received rather than with no sun at all, which would make every night a winter night.
            hourly = {t: v for t, v in ((datetime.fromisoformat(k), v) for k, v in self.forecast_archive.items())
                      if t >= now - timedelta(hours=1)}
            if hourly and not self._forecast_out:
                self._forecast_out = True
                self.event("The solar forecast is unavailable; planning with the last one received.",
                           logging.WARNING)
        self.pv = PvForecast(hourly, self.shade, self.tz)

    def car_status(self, now: datetime) -> dict:
        s = self.car_state
        source = {"ChargerPowerSource": "charger sensor", "LoadStepSource": "house consumption"}.get(
            type(self.car).__name__)
        return {"enabled": source is not None, "source": source, "charging": s.charging,
                "power_kw": round(s.power_kw, 2) if s.charging else 0.0,
                "session_kwh": round(self._car_session[1], 1) if self._car_session else None,
                "month_kwh": round(self.car_months.get(now.astimezone(self.tz).strftime("%Y-%m"), 0.0), 1),
                "shaving": bool(s.charging and self.solis and self.solis.mode == "peak_shave"),
                "can_shave": self.can_peak_shave, "soc": s.soc, "plugged": s.plugged,
                "fill": self.fill_on, "fill_state": self._fill_state(),
                "fill_until": ((datetime.fromisoformat(self.fill_since) + timedelta(hours=FILL_MAX_H)).isoformat()
                               if self.fill_on and self.fill_since else None)}

    def _fill_state(self) -> str | None:
        if not self.fill_on:
            return None
        if not self.control_on or not self.solis:
            return "no_control"
        if self.car_state.charging:
            return "car"
        if self.solis.mode == "charge" and self.readings and self.readings.soc < 100:
            return "filling"
        if self.readings and self.readings.soc >= FILL_AGAIN:
            return "holding"
        return "waiting"

    @property
    def today_factor(self) -> float:
        """Measured solar against the forecast over the last hours (1.0: as forecast, or no data)."""
        return round(self.pv.nowcast[0], 2) if self.pv.nowcast else 1.0

    def _archive_hours(self) -> dict[datetime, float]:
        return {datetime.fromisoformat(k): v for k, v in self.forecast_archive.items()}

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
        current = sticky_action(self.plan, now, soc, self.o.battery_reserve_soc) if self.readings else None
        # Today's weather against the forecast, from the quarters measured so far.
        self.pv.observe(self.records, now, past=self._archive_hours(), export_limit_kw=self.o.export_limit_kw)
        # While the car charges, the next hour has its power on top of the house (how long it goes
        # on is not known: the plan is redone every few minutes from the real battery level).
        car_kw = self.car_state.power_kw if self.car_state.charging else 0.0
        car_end = now + timedelta(hours=1)

        def car_kwh(a: datetime, b: datetime) -> float:
            return car_kw * max((min(b, car_end) - max(a, now)).total_seconds(), 0.0) / 3600

        self.plan = make_plan(now, self.tz, self.o, tar, self.price_slots,
                              lambda a, b: self.load.kwh(a, b, self.tz) + car_kwh(a, b),
                              self.pv.kwh, soc, market_avg=self.market_avg_month(now),
                              curtail_negative=self.blocks_negative_export, forecast_until=self.pv.until,
                              peak_kw=lambda a, b: self.load.peak_kw(a, b, self.tz) + (car_kw if car_kwh(a, b) > 0 else 0.0),
                              month_peak_kw=self.peaks.month_peak(now, self.tz), current_action=current,
                              peak_shave=self.can_peak_shave, solar_hold=self.can_solar_hold)
        self._log_plan(now)
        avg = self.market_avg_month(now)
        self.trading = {k: trading.margins(self.o, k, self.price_slots, now, self.tz, avg) for k in KINDS}
        boost, seen = self.plan.boost, self.boost_seen
        # The plan is redone every few minutes while a boost runs: announce a window once, unless
        # a new one starts after it or the level goes up clearly.
        if boost and seen and boost["from"] < seen.get("to", "") and boost["limit_kw"] < seen.get("limit_kw", 0) + 0.5:
            seen["to"] = max(seen["to"], boost["to"])
        elif boost:
            self.boost_seen = {"from": boost["from"], "to": boost["to"], "limit_kw": boost["limit_kw"]}
            a = datetime.fromisoformat(boost["from"]).astimezone(self.tz)
            z = datetime.fromisoformat(boost["to"]).astimezone(self.tz)
            why = (f"it earns about €{boost['gain_eur']:.2f}, more than the €{boost['peak_cost_eur']:.2f} the higher "
                   "capacity peak costs" if boost["peak_cost_eur"] > 0 else
                   f"this month already has a peak that high, so it costs nothing extra (earns about €{boost['gain_eur']:.2f})")
            self.event(f"Plan: charging above the import limit, up to {boost['limit_kw']:g} kW, {a:%H:%M}–{z:%H:%M}: {why}.")

    def _log_plan(self, now: datetime):
        """Keep what this plan predicts: for the day (the first plan after the nightly learning)
        and for this clock hour (the first plan in it)."""
        try:
            local = now.astimezone(self.tz)
            day = local.date()
            start, end = H.day_bounds(day, self.tz)
            entry = self.day_logs.setdefault(day.isoformat(), {"day_ahead": None, "hours": {}})
            load = lambda a, b: self.load.kwh(a, b, self.tz)  # noqa: E731
            if entry.get("day_ahead") is None and self.last_nightly == day.isoformat():
                entry["day_ahead"] = H.day_ahead(self.plan, self.pv, load, start, end)
            hour = local.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            key = str(H.minutes(hour, start))
            if key not in entry["hours"]:
                entry["hours"][key] = H.hour_ahead(self.plan, self.pv, load, hour, start)
            for d in sorted(self.day_logs)[:-H.OPEN_DAYS]:
                del self.day_logs[d]
        except Exception as e:  # noqa: BLE001 - the history must never stop the planning
            log.warning("Keeping the plan for the history failed: %s", e)

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
        if recs:              # (none on the first day after install: nothing measured yet)
            self._close_day(y)
            n_shade = self.shade.learn_records(recs, self.forecast_archive, self.tz)
            n_load = self.load.learn_records(recs, self.tz)
            self.efficiency.learn_records(recs)
            self.event(f"Learned from {y}: {n_load} hours of consumption, {n_shade} hours of solar vs forecast.")
            if self.price_slots or self.o.contract == "fixed":
                # The replay is CPU work: run it in a thread on copies, apply the results here.
                soc0 = {k: self.comparison.sim_soc.get(k) for k in KINDS}
                peak0 = {k: self.comparison.month(y.strftime("%Y-%m"))["replay"][k]["peak_kw"] for k in KINDS}
                block = self.blocks_negative_export
                results = await asyncio.get_running_loop().run_in_executor(
                    None, self._replay, list(recs), start, list(self.price_slots), dict(self.forecast_archive),
                    soc0, block, peak0)
                self._apply_replay(y, results, block)
            self.comparison.prune()
        # Plan again with what was just learned; this plan is also the day-ahead prediction.
        self.replan(now)
        self.save_state()

    def _close_day(self, day: date):
        """Store the day's predictions and measurements in the history."""
        try:
            doc = H.build_day(day, self.tz, self.records, self.day_logs.get(day.isoformat()))
            self.history.write(doc)
            self.history.prune(day)
            self.day_logs.pop(day.isoformat(), None)
        except Exception as e:  # noqa: BLE001
            self.event(f"Storing the history of {day} failed: {e}", logging.WARNING)

    def _replay(self, recs: list[QuarterRecord], start: datetime, price_slots, forecast_archive, soc0: dict,
                block_negative: bool = False, peak0: dict | None = None):
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
            res = replay_day(recs, kind, self.o, self.tz, price_slots, self.load, pv_fc.kwh, s0, avg,
                             block_negative=block_negative, month_peak_kw=(peak0 or {}).get(kind, 0.0),
                             peak_shave=self.can_peak_shave, solar_hold=self.can_solar_hold,
                             on_replan=lambda past, t: pv_fc.observe(past, t, export_limit_kw=self.o.export_limit_kw))
            if res is None:
                return None
            results[kind] = res
        return results

    def _apply_replay(self, day: date, results: dict | None, block_negative: bool = False):
        if results is None:
            self.event(f"Replay of {day} skipped: too much data missing.")
            return
        key = day.strftime("%Y-%m")
        both = len(results) == len(KINDS)
        for kind, res in results.items():
            if both:          # a day replayed for one contract only would count for that one alone
                self.comparison.add_replay(key, kind, res)
            self.comparison.sim_soc[kind] = res["soc_end"]
        if both:
            self.comparison.month(key)["replay_days"] += 1
            f, d = results["fixed"]["cost_c"] / 100, results["dynamic"]["cost_c"] / 100
            blocked = "; export blocked at negative prices" if block_negative else ""
            self.event(f"Replayed {day}: fixed €{f:.2f}, dynamic €{d:.2f} (energy and battery wear{blocked}).")

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
        if local.day <= 7:            # (on a later day too, when the app was not running on the 1st)
            prev = (local.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
            if prev in self.comparison.months:
                r = self.comparison.report(prev, self.o)
                await self.alert(f"Contract check {prev}", self.verdict_text(r), key=f"monthly-{prev}")
        end = self.o.fixed_end_date
        if end and self.o.contract == "fixed":
            days = (end - local.date()).days
            due = [d for d in (42, 14, 3) if 0 <= days <= d]     # the most urgent reminder not yet sent
            if due:
                mark = min(due)
                s = self.comparison.summary(self.o, now, self.tz)
                diff = (s.get("with_strategy") or s.get("same_usage") or {}).get("dynamic_minus_fixed_eur")
                extra = ""
                if diff is not None:
                    extra = (f" Over the stored months dynamic would have been {'cheaper' if diff < 0 else 'dearer'}"
                             f" by about €{abs(diff):.0f}.")
                await self.alert(f"Fixed price ends in {days} days",
                                 f"Your fixed price ends on {end}. When the new rates arrive, enter them in the app's "
                                 f"settings for a new comparison.{extra}", key=f"renewal-{end}-{mark}")
        avg30 = self.market_avg_30d()
        night_be = breakeven_eur_mwh(self.o.fixed_night_rate, self.o)
        day_be = breakeven_eur_mwh(self.o.fixed_day_rate, self.o)
        if avg30 is not None and night_be is not None and self.o.contract == "fixed" and len(self.daily_market) >= 30:
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
            "plan_cost_eur": round(sum(st.cost_c for st in p.steps) / 100, 2) if p else None,
            "boost_kw": p.boost["limit_kw"] if p and p.boost else None,
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
                "forecast_kwh": round(self.pv.day_total(day, corrected=False), 1),
                **({"today_factor": self.today_factor} if name == "today" else {})}))
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
        car = self.car_status(now)
        if car["enabled"]:
            sensors.append(("sensor.battery_pilot_car_power", car["power_kw"], {
                "friendly_name": "Battery Pilot car charging power", "unit_of_measurement": "kW", "device_class": "power",
                "state_class": "measurement", "icon": "mdi:car-electric", "charging": car["charging"],
                "source": car["source"], "month_kwh": car["month_kwh"], "peak_shaving": car["shaving"]}))
        eff = self.efficiency.report(self.o.round_trip_efficiency_pct)
        sensors.append(("sensor.battery_pilot_efficiency",
                        "unknown" if eff["round_trip_pct"] is None else eff["round_trip_pct"], {
                            "friendly_name": "Battery Pilot measured round-trip efficiency",
                            "unit_of_measurement": "%", "icon": "mdi:battery-sync", **eff}))
        for eid, state, attrs in sensors:
            try:
                await self.ha.set_state(eid, state, attrs)
            except Exception as e:  # noqa: BLE001
                log.debug("publish %s failed: %s", eid, e)

    # ---------------------------------------------------------------- dashboard
    def status(self) -> dict:
        now = self.clock()
        r = self.readings
        night_be = breakeven_eur_mwh(self.o.fixed_night_rate, self.o)
        day_be = breakeven_eur_mwh(self.o.fixed_day_rate, self.o)
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
            "trading": self.trading,
            "car": self.car_status(now),
            "solar": {"today": round(self.pv.day_total(now.astimezone(self.tz).date()), 1),
                      "tomorrow": round(self.pv.day_total(now.astimezone(self.tz).date() + timedelta(days=1)), 1),
                      "today_factor": self.today_factor,
                      "shade": self.shade.to_json()["factors"], "shade_months": self.shade.month_summary(),
                      "shade_kwh": [[round(w, 1) for w in row] for row in self.shade.weights]},
            "load": {"daily_kwh": self.load.daily_kwh(), "kw": self.load.to_json()["kw"]},
            "peaks": {"months": self.peaks.months, "limit_kw": self.o.import_limit_kw,
                      "rolling_kw": self.peaks.rolling_average(self.o.capacity_min_kw)},
            "comparison": self.comparison.summary(self.o, now, self.tz),
            "market_30d": self.market_avg_30d(),
            "efficiency": self.efficiency.report(self.o.round_trip_efficiency_pct),
            "breakeven": {"night": None if night_be is None else round(night_be),
                          "day": None if day_be is None else round(day_be)},
            "events": list(reversed(self.events[-30:])),
        }

    # ---------------------------------------------------------------- planned vs actual
    def history_day(self, day: date) -> dict | None:
        """A stored day, or a day still in memory (today, live; yesterday until it is closed)."""
        now = self.clock()
        today = now.astimezone(self.tz).date()
        key = day.isoformat()
        if day != today:
            doc = self.history.read(day)
            if doc is not None or key not in self.day_logs:
                return doc
        live = None
        if day == today:
            start, end = H.day_bounds(day, self.tz)
            live = {"now": H.minutes(now, start), "soc": self.readings.soc if self.readings else None,
                    "plan": H.plan_points(self.plan, now, end, start) if self.plan else []}
        return H.build_day(day, self.tz, self.records, self.day_logs.get(key), live)

    def history_index(self) -> dict:
        """Summaries of every stored day, plus the days still in memory."""
        today = self.clock().astimezone(self.tz).date()
        days = {s["date"]: s for s in self.history.index()}
        for key in set(self.day_logs) | {today.isoformat()}:
            if key not in days and key <= today.isoformat():
                doc = self.history_day(date.fromisoformat(key))
                if doc:
                    days[key] = doc["summary"]
        return {"today": today.isoformat(), "tz": str(self.tz), "days": [days[k] for k in sorted(days)]}

    # ---------------------------------------------------------------- main loop
    async def step(self):
        """One round of the main loop (called every TICK_S seconds)."""
        now = self.clock()
        await self.tick()
        idle = self._refresh_task is None or self._refresh_task.done()
        if idle and self._due("refresh", now, REFRESH_S):
            # Prices and the forecast can take long to read (the forecast websocket up to a minute):
            # in the background, so the 10-second control loop keeps running meanwhile.
            self._refresh_task = asyncio.create_task(self._background_refresh())
            await asyncio.sleep(0)
        await self.nightly()
        await self.daily_alerts()
        if self._due("publish", now, PUBLISH_S):
            await self.publish()
        if self._due("save", now, SAVE_S):
            self.save_state()

    async def _background_refresh(self):
        try:
            await self.refresh()
        except Exception as e:  # noqa: BLE001
            self._error(e)

    async def reach_ha(self) -> bool:
        """Ping Home Assistant; while it does not answer, say why (logged once per reason)."""
        if await self.ha.ping():
            self._ha_wait = None
            return True
        reason = getattr(self.ha, "last_error", None) or "no answer"
        if "HTTP 401" in reason:
            reason += " - the app has no valid Supervisor token"
        self.problem = f"waiting for Home Assistant ({reason})"
        if reason != self._ha_wait:
            self._ha_wait = reason
            self.event(f"Waiting for Home Assistant: {reason}", logging.WARNING)
        return False

    def _due(self, name: str, now: datetime, every_s: int) -> bool:
        last = self._last.get(name)
        if last is None or (now - last).total_seconds() >= every_s:
            self._last[name] = now
            return True
        return False

    def healthy(self) -> bool:
        """For the Docker health check: the main loop is still going round (it may be waiting
        for Home Assistant; a restart would not help then)."""
        return monotonic() - self.alive_at < HEALTH_S

    async def run(self, stop: asyncio.Event):
        while not stop.is_set():
            self.alive_at = monotonic()
            try:
                if not self.ready:
                    if not await self.reach_ha():
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
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        if self.solis and self.solis.mode in ACTIVE:
            try:
                await self.solis.self_use(self.clock().astimezone(self.tz), "Battery Pilot stopped")
            except Exception as e:  # noqa: BLE001
                log.warning("Could not return the inverter to self-use: %s", e)
            self._save_inverter()
        self.save_state()


async def _sleep(stop: asyncio.Event, seconds: float):
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except TimeoutError:
        pass
