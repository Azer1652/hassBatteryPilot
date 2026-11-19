"""An in-memory Home Assistant with a house, a battery and a Solis inverter.

It has the same methods as `pilot.ha.HA`, records every service call, and
`advance()` moves the physics forward: the inverter follows its time-of-use
slots and currents the way the Solis does, the battery SOC changes, and the
grid/house/PV power sensors are updated.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pilot.ha import HAUnreachable

TZ = ZoneInfo("Europe/Brussels")
V = 51.2
CAP = 16.0
# A fixed contract whose night rate is low enough for night charging to pay after losses and wear:
# with the default rates it does not, and a winter night then gets no charge target.
CHEAP_NIGHT = {"fixed_night_rate": 8.0}


# The rates the planner's behaviour tests were tuned on (the defaults until 2.11.1). A test that
# checks what the plan does at a given price level passes these, so new default rates do not move it.
RATES_EARLY_2026 = {"grid_fees_c_kwh": 11.96, "dynamic_buy_add": 1.0, "dynamic_sell_add": -1.188,
                    "dynamic_yearly_fee_eur": 100.0}


def st(state, unit=None, **attrs):
    a = dict(attrs)
    if unit:
        a["unit_of_measurement"] = unit
    return {"state": str(state), "attributes": a}


class FakeHA:
    def __init__(self, now: datetime, soc=30.0, load=lambda t: 0.5, pv=lambda t: 0.0, forecast=None,
                 prices=None, with_helpers=True, peak_shaving=False, feed_in_priority=False, force_action=False,
                 battery_power=False, inverter_draw_kw=0.0, marstek=False,
                 p1_peak=0):
        self.now = now
        # marstek: a Solax (grid, house, solar) and a Marstek Venus seen by the Marstek Local API and the
        # Marstek Cloud integrations, instead of a Solis. local_down / cloud_down: that link is silent (its
        # sensors keep their last values; the local actions fail).
        # marstek="taurgis": the local integration is taurgis/has-marstek-local-api (domain marstek, an
        # operating-mode select, power + discharging, entities unavailable when silent) instead of jaapp's.
        self.marstek = marstek
        self.local_domain = "marstek" if marstek == "taurgis" else "marstek_local_api"
        self.local_down = False
        self.cloud_down = False
        self.local_silent_s = 0.0
        self.cloud_report = now
        self.marstek_lag_s = 0                  # a new passive power takes effect this much later
        self.pending = None
        self.call_timeouts: list = []
        self._q = (None, 0.0)                   # P1 meter: (quarter start, kWh imported so far)
        self.inverter_draw_kw = inverter_draw_kw   # the inverter's own use, always from the grid
        self.force_action = force_action        # the Solis Modbus force charge/discharge actions exist
        self.forced: tuple[str, float, datetime] | None = None   # (charge|discharge, W, until) while one runs
        self.soc = soc
        self.load_fn = load
        self.pv_fn = pv
        self.forecast = forecast or {}          # {UTC hour: kWh}
        self.calls: list[tuple[str, str, dict]] = []
        self.supervisor_calls: list[tuple] = []     # (method, path, body) sent to the Supervisor
        self.supervisor_error: str | None = None    # set: the Supervisor refuses
        self.published: dict[str, dict] = {}
        self.check_ok = True
        self.history_data: dict = {}
        self.import_kw = 0.0
        self.override: dict[str, str] = {}      # entity -> state shown instead of the physics (unavailable, frozen)
        self.fail = None                        # (domain, service, data) -> True to make that call fail
        s = {
            "sensor.solis_grid_import_power": st(0, "W"),
            "sensor.solis_grid_export_power": st(0, "W"),
            "sensor.solis_household_load_power": st(0, "W"),
            "sensor.solis_total_pv_power": st(0, "W"),
            "sensor.solis_battery_soc": st(soc, "%"),
            "sensor.solis_battery_voltage": st(V, "V"),
            "switch.solis_time_of_use": st("off"),
            "switch.solis_allow_grid_to_charge_the_battery": st("off"),
            "number.solis_time_charging_charge_current": st(50, "A"),
            "number.solis_time_charging_discharge_current": st(50, "A"),
            "number.solis_export_power_limit": st(5000, "W"),
            "time.solis_time_charging_charge_start_slot_1": st("00:00:00"),
            "time.solis_time_charging_charge_end_slot_1": st("00:00:00"),
            "time.solis_time_charging_discharge_start_slot_1": st("00:00:00"),
            "time.solis_time_charging_discharge_end_slot_1": st("00:00:00"),
            "sensor.power_production_now": st(0, "W"),
        }
        if marstek:
            s = {
                "sensor.p1_meter_power": st(0, "W"),          # HomeWizard P1: + import
                "sensor.p1_meter_average_demand": st(0, "W"),
                "sensor.p1_meter_peak_demand_current_month": st(p1_peak, "W"),
                "sensor.solax_grid_import": st(0, "W"),
                "sensor.solax_grid_export": st(0, "W"),
                "sensor.solax_house_load": st(0, "W"),          # + what the Marstek charges, - what it gives
                "sensor.solax_house_load_alt": st(0, "W"),
                "sensor.solax_pv_power_total": st(0, "W"),
                "number.solax_export_control_user_limit": st(5000, "W"),
                "sensor.power_production_now": st(0, "W"),
                "sensor.marstek_venuse_state_of_charge": st(soc, "%"),
                "sensor.marstek_venuse_power": st(0, "W"),      # + charging
                "sensor.marstek_venuse_grid_power": st(0, "W"),
                "sensor.marstek_venuse_voltage": st(V, "V"),
                "sensor.marstek_venuse_last_message_received": st(0, "s"),
                "button.marstek_venuse_auto_mode": st("unknown"),
                "button.marstek_venuse_manual_mode": st("unknown"),
                "sensor.venus_state_of_charge": st(soc, "%"),
                "sensor.venus_charge_power": st(0, "W"),
                "sensor.venus_discharge_power": st(0, "W"),
                "sensor.venus_total_charge": st(0, "kWh"),
                "sensor.venus_report_time": st(int(now.timestamp())),
            }
            self.registry = [
                {"entity_id": eid, "platform": plat, "unique_id": uid, "device_id": dev, "disabled_by": None}
                for eid, plat, uid, dev in (
                    ("sensor.marstek_venuse_state_of_charge", "marstek_local_api", "aabbcc_battery_soc", "dev1"),
                    ("sensor.marstek_venuse_power", "marstek_local_api", "aabbcc_battery_power", "dev1"),
                    ("sensor.marstek_venuse_grid_power", "marstek_local_api", "aabbcc_grid_power", "dev1"),
                    ("sensor.marstek_venuse_voltage", "marstek_local_api", "aabbcc_battery_voltage", "dev1"),
                    ("sensor.marstek_venuse_last_message_received", "marstek_local_api",
                     "aabbcc_last_message_received", "dev1"),
                    ("button.marstek_venuse_auto_mode", "marstek_local_api", "aabbcc_auto_mode_button", "dev1"),
                    ("button.marstek_venuse_manual_mode", "marstek_local_api", "aabbcc_manual_mode_button", "dev1"),
                    ("sensor.venus_state_of_charge", "marstek_cloud", "123_soc", "dev2"),
                    ("sensor.venus_charge_power", "marstek_cloud", "123_charge", "dev2"),
                    ("sensor.venus_discharge_power", "marstek_cloud", "123_discharge", "dev2"),
                    ("sensor.venus_total_charge", "marstek_cloud", "123_total_charge", "dev2"),
                    ("sensor.venus_report_time", "marstek_cloud", "123_report_time", "dev2"),
                )]
            if marstek == "taurgis":
                for eid in [e for e in s if e.startswith(("sensor.marstek_venuse", "button.marstek_venuse"))]:
                    del s[eid]
                s["sensor.marstek_venuse_battery_level"] = st(soc, "%")
                s["sensor.marstek_venuse_battery_power"] = st(0, "W")      # + discharging
                s["select.marstek_venuse_operating_mode"] = st("auto", options=["auto", "ai", "manual", "passive"])
                self.registry = [e for e in self.registry if e["platform"] != "marstek_local_api"] + [
                    {"entity_id": eid, "platform": "marstek", "unique_id": uid, "device_id": "dev1",
                     "disabled_by": None}
                    for eid, uid in (("sensor.marstek_venuse_battery_level", "aabbcc_battery_soc"),
                                     ("sensor.marstek_venuse_battery_power", "aabbcc_battery_power"),
                                     ("select.marstek_venuse_operating_mode", "aabbcc_operating_mode"))]
        if peak_shaving:        # the Solis peak-shaving mode: grid up to the set power, the battery above it
            s["switch.solis_peak_shaving_mode"] = st("off")
            s["number.solis_peak_max_usable_grid_power"] = st(5000, "W")
            s["number.solis_peak_baseline_soc"] = st(50, "%")
        if feed_in_priority:    # the Solis feed-in priority: solar surplus is exported, not stored
            s["switch.solis_feed_in_priority"] = st("off")
        if battery_power:       # Solis "Battery Power Net" (DC, + discharging), and the unsigned one beside it
            s["sensor.solis_battery_power_net"] = st(0, "W")
            s["sensor.solis_battery_power"] = st(0, "W")
        if prices is not None:
            s["sensor.current_electricity_market_price"] = st(0.1, "€/kWh", prices=prices)
        if with_helpers:
            s["input_boolean.battery_pilot_control"] = st("off")
            s["input_boolean.battery_pilot_block_negative_export"] = st("off")
            s["input_boolean.battery_pilot_fill_battery_for_car"] = st("off")
            s["input_boolean.battery_pilot_force_peak_shaving"] = st("off")
        self.s = s
        self.helpers_created: list[str] = []
        self.advance(timedelta(0))

    # ---------------------------------------------------------------- HA client interface
    ping_error: str | None = None        # set to make HA unreachable
    last_error: str | None = None
    away = False                         # set: Home Assistant restarts (the Supervisor proxy answers 502)

    def _check_away(self, what: str):
        if self.away:
            raise HAUnreachable(f"{what}: HTTP 502 502: Bad Gateway")

    async def ping(self):
        if self.away:
            self.last_error = "GET /: HTTP 502 502: Bad Gateway"
            return False
        self.last_error = self.ping_error
        return self.ping_error is None

    async def config(self):
        return {"time_zone": "Europe/Brussels", "latitude": 51.2, "longitude": 4.4}

    async def states(self):
        return {k: {"entity_id": k, **v} for k, v in self.s.items()}

    async def state(self, eid):
        self._check_away(f"GET /states/{eid}")
        v = self.s.get(eid)
        if v is not None and eid in self.override:
            v = {**v, "state": self.override[eid]}
        return None if v is None else {"entity_id": eid, **v}

    async def supervisor(self, method, path, body=None):
        if self.supervisor_error:
            raise RuntimeError(self.supervisor_error)
        self.supervisor_calls.append((method, path, body))
        return {"result": "ok"}

    async def services(self):
        out = [{"domain": "notify", "services": {"mobile_app_phone": {}, "persistent_notification": {}}}]
        if self.marstek:
            out.append({"domain": self.local_domain, "services": {"set_passive_mode": {}, "request_data_sync": {}}})
        if self.force_action:
            out.append({"domain": "solis_modbus", "services": {
                "solis_force_battery_charge": {}, "solis_force_battery_discharge": {},
                "solis_stop_force_charge_discharge": {}, "solis_write_holding_register": {}}})
        return out

    async def call(self, domain, service, data=None, timeout=30):
        self._check_away(f"POST /services/{domain}/{service}")
        data = data or {}
        if self.fail and self.fail(domain, service, data):
            raise RuntimeError("Modbus timeout")
        self.calls.append((domain, service, data))
        eid = data.get("entity_id")
        if domain in ("switch", "input_boolean") and isinstance(eid, str):
            self.s[eid]["state"] = "on" if service == "turn_on" else "off"
        elif domain == "number" and service == "set_value":
            self.s[eid]["state"] = str(data["value"])
        elif domain == "time" and service == "set_value":
            self.s[eid]["state"] = data["time"]
        elif domain == self.local_domain or (domain in ("button", "select") and "marstek" in str(eid)):
            if self.local_down:
                # the integration retries for 45 s; the app's client gives up at its timeout
                self.call_timeouts.append(timeout)
                raise HAUnreachable(f"POST /services/{domain}/{service}: TimeoutError") from TimeoutError()
            if domain == "select":
                assert data["option"] in self.s[eid]["attributes"]["options"], data
                self.s[eid]["state"] = data["option"]
                if data["option"].lower() == "auto":
                    self.forced = self.pending = None
            elif domain == "button":
                if eid.endswith("auto_mode"):
                    self.forced = self.pending = None
            else:
                assert data["device_id"] == "dev1", data
                assert 1 <= data["duration"] <= 86400 and -10000 <= data["power"] <= 10000, data
                if self.marstek == "taurgis":
                    assert -5000 <= data["power"] <= 5000, data
                    self.s["select.marstek_venuse_operating_mode"]["state"] = "passive"
                kind = "charge" if data["power"] < 0 else "discharge"
                forced = (kind, float(abs(data["power"])), self.now + timedelta(seconds=data["duration"]))
                if self.marstek_lag_s:
                    self.pending = (forced, self.now + timedelta(seconds=self.marstek_lag_s))
                else:
                    self.forced = forced
        elif domain == "solis_modbus":
            if not self.force_action:
                raise RuntimeError(f"Action solis_modbus.{service} not found")
            if service == "solis_stop_force_charge_discharge":
                self.forced = None
            else:
                minutes = int(data["duration_minutes"])
                assert 1 <= minutes <= 30, minutes          # the integration's schema
                kind = "charge" if service == "solis_force_battery_charge" else "discharge"
                self.forced = (kind, float(data["power_watts"]), self.now + timedelta(minutes=minutes))
        elif domain == "automation" and service == "turn_off":
            for e in eid if isinstance(eid, list) else [eid]:
                self.s[e]["state"] = "off"

    async def set_state(self, eid, state, attributes=None):
        self._check_away(f"POST /states/{eid}")
        self.published[eid] = {"state": str(state), "attributes": attributes or {}}

    async def check_config(self):
        return self.check_ok, "" if self.check_ok else "boom"

    async def history(self, ids, start, end):
        return {i: self.history_data[i] for i in ids if i in self.history_data}

    async def ws(self, *commands):
        if self.away:
            raise HAUnreachable("websocket: HTTP 502 Bad Gateway")
        out = []
        for c in commands:
            if c["type"] == "input_boolean/list":
                out.append([])
            elif c["type"] == "input_boolean/create":
                eid = "input_boolean." + c["name"].lower().replace(" ", "_")
                self.s[eid] = st("off")
                self.helpers_created.append(eid)
                out.append({"id": eid})
            elif c["type"] == "config/entity_registry/list":
                out.append(getattr(self, "registry", []))
            elif c["type"] == "energy/solar_forecast":
                out.append({"x": {"wh_hours": {h.isoformat(): kwh * 1000 for h, kwh in self.forecast.items()}}})
            else:
                raise RuntimeError(c["type"])
        return out

    # ---------------------------------------------------------------- physics
    def _in_slot(self, kind, local):
        start = self.s[f"time.solis_time_charging_{kind}_start_slot_1"]["state"]
        end = self.s[f"time.solis_time_charging_{kind}_end_slot_1"]["state"]
        hhmmss = local.strftime("%H:%M:%S")
        if start > end:                                  # a slot across midnight (23:00-00:00)
            return hhmmss >= start or hhmmss < end
        return start != end and start <= hhmmss < end

    def mode(self):
        local = self.now.astimezone(TZ)
        if getattr(self, "pending", None) and self.now >= self.pending[1]:
            self.forced, self.pending = self.pending[0], None
        if self.forced and self.now >= self.forced[2]:      # the timeout: back to the inverter's own mode
            self.forced = None
        if self.forced:
            return "force_" + self.forced[0]
        if self.marstek:
            return "self_use"
        if self.s["switch.solis_time_of_use"]["state"] != "on":
            if self.s.get("switch.solis_feed_in_priority", {}).get("state") == "on":
                return "solar_hold"
            shaving = self.s.get("switch.solis_peak_shaving_mode", {}).get("state") == "on"
            return "peak_shave" if shaving else "self_use"
        if self._in_slot("charge", local) and self.s["switch.solis_allow_grid_to_charge_the_battery"]["state"] == "on":
            return "charge"
        if self._in_slot("discharge", local):
            return "discharge"
        return "self_use"

    def advance(self, dt: timedelta):
        h = dt.total_seconds() / 3600
        load, pv = self.load_fn(self.now), self.pv_fn(self.now)
        mode = self.mode()
        net = load - pv
        room, avail = (100 - self.soc) / 100 * CAP, max(self.soc - 10, 0) / 100 * CAP
        if mode in ("charge", "force_charge"):
            amps_kw = (self.forced[1] / 1000 if mode == "force_charge"
                       else float(self.s["number.solis_time_charging_charge_current"]["state"]) * V / 1000)
            ch = min(amps_kw, room / h if h else 9e9)
            ch = max(ch, min(-net, 2.56)) if net < 0 else ch
            batt = ch
            grid = net + ch / 0.97
        elif mode in ("discharge", "force_discharge"):
            dis = min(self.forced[1] / 1000 if mode == "force_discharge"
                      else float(self.s["number.solis_time_charging_discharge_current"]["state"]) * V / 1000,
                      avail / h if h else 9e9)
            batt = -dis
            grid = net - dis * 0.97
        elif mode == "peak_shave" and net > 0:
            limit = float(self.s["number.solis_peak_max_usable_grid_power"]["state"]) / 1000
            base = float(self.s["number.solis_peak_baseline_soc"]["state"])
            above = max(min(avail, (self.soc - base) / 100 * CAP), 0.0)    # held at the baseline
            dis = min(max(net - limit, 0) / 0.97, 4.6, above / h if h else 9e9)
            batt, grid = -dis, net - dis * 0.97
            if self.soc < base:   # refilled from the grid
                ch = min(max(limit - net, 0) * 0.97, 2.56, room / h if h else 9e9)
                batt, grid = ch, net + ch / 0.97
        elif mode == "solar_hold":      # surplus is exported, a deficit comes from the grid; the battery is idle
            batt, grid = 0.0, net
        else:
            if net > 0:
                dis = min(net / 0.97, 4.6, avail / h if h else 9e9)
                batt, grid = -dis, net - dis * 0.97
            else:
                ch = min(-net * 0.97, 2.56, room / h if h else 9e9)
                batt, grid = ch, net + ch / 0.97
        self.soc = min(max(self.soc + batt * h / CAP * 100, 0), 100)
        grid += self.inverter_draw_kw
        self.import_kw = max(grid, 0)
        self.now += dt
        if self.marstek:
            self._marstek_sensors(grid, batt, dt)
            return
        self.s["sensor.solis_grid_import_power"]["state"] = str(round(max(grid, 0) * 1000))
        self.s["sensor.solis_grid_export_power"]["state"] = str(round(max(-grid, 0) * 1000))
        self.s["sensor.solis_household_load_power"]["state"] = str(round(self.load_fn(self.now) * 1000))
        self.s["sensor.solis_total_pv_power"]["state"] = str(round(self.pv_fn(self.now) * 1000))
        self.s["sensor.solis_battery_soc"]["state"] = str(round(self.soc, 1))
        if "sensor.solis_battery_power_net" in self.s:     # DC, + discharging
            self.s["sensor.solis_battery_power_net"]["state"] = str(round(-batt * 1000))

    def _marstek_sensors(self, grid, batt, dt):
        w = {"sensor.solax_grid_import": max(grid, 0), "sensor.solax_grid_export": max(-grid, 0),
             # the Solax sees the Marstek on the house side: its charging is house load
             "sensor.solax_house_load": self.load_fn(self.now) + batt,
             "sensor.solax_pv_power_total": self.pv_fn(self.now)}
        for eid, kw in w.items():
            self.s[eid]["state"] = str(round(kw * 1000))
        start = self.now.replace(minute=self.now.minute - self.now.minute % 15, second=0, microsecond=0)
        q, kwh = self._q
        kwh = (kwh if q == start else 0.0) + max(grid, 0) * dt.total_seconds() / 3600
        self._q = (start, kwh)
        done = (self.now - start).total_seconds() / 3600
        self.s["sensor.p1_meter_power"]["state"] = str(round(grid * 1000))
        self.s["sensor.p1_meter_average_demand"]["state"] = str(round(kwh / done * 1000) if done > 0 else 0)
        if self.marstek == "taurgis":
            lvl, pw = "sensor.marstek_venuse_battery_level", "sensor.marstek_venuse_battery_power"
            if self.local_down:     # after three failed polls the entities turn unavailable
                self.s[lvl]["state"] = self.s[pw]["state"] = "unavailable"
            else:
                self.s[lvl]["state"] = str(round(self.soc))
                self.s[pw]["state"] = str(round(-batt * 1000))
                if self.forced is None and self.s["select.marstek_venuse_operating_mode"]["state"] == "passive":
                    self.s["select.marstek_venuse_operating_mode"]["state"] = "auto"
        elif self.local_down:
            self.local_silent_s += dt.total_seconds()
        else:
            self.local_silent_s = 0.0
            self.s["sensor.marstek_venuse_state_of_charge"]["state"] = str(round(self.soc))
            self.s["sensor.marstek_venuse_power"]["state"] = str(round(batt * 1000))
        if "sensor.marstek_venuse_last_message_received" in self.s:
            self.s["sensor.marstek_venuse_last_message_received"]["state"] = str(int(self.local_silent_s))
        if not self.cloud_down:
            self.cloud_report = self.now
            self.s["sensor.venus_state_of_charge"]["state"] = str(round(self.soc))
            self.s["sensor.venus_charge_power"]["state"] = str(round(max(batt, 0) * 1000))
            self.s["sensor.venus_discharge_power"]["state"] = str(round(max(-batt, 0) * 1000))
        self.s["sensor.venus_report_time"]["state"] = str(int(self.cloud_report.timestamp()))

    def solis_calls(self):
        return [c for c in self.calls if c[0] in ("switch", "number", "time", "solis_modbus")]


def utc(*a):
    return datetime(*a, tzinfo=TZ).astimezone(timezone.utc)
