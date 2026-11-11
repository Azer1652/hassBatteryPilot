"""An in-memory Home Assistant with a house, a battery and a Solis inverter.

It has the same methods as `pilot.ha.HA`, records every service call, and
`advance()` moves the physics forward: the inverter follows its time-of-use
slots and currents the way the Solis does, the battery SOC changes, and the
grid/house/PV power sensors are updated.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Brussels")
V = 51.2
CAP = 16.0
# A fixed contract whose night rate is low enough for night charging to pay after losses and wear:
# with the default rates it does not, and a winter night then gets no charge target.
CHEAP_NIGHT = {"fixed_night_rate": 8.0}


def st(state, unit=None, **attrs):
    a = dict(attrs)
    if unit:
        a["unit_of_measurement"] = unit
    return {"state": str(state), "attributes": a}


class FakeHA:
    def __init__(self, now: datetime, soc=30.0, load=lambda t: 0.5, pv=lambda t: 0.0, forecast=None,
                 prices=None, with_helpers=True, peak_shaving=False, feed_in_priority=False):
        self.now = now
        self.soc = soc
        self.load_fn = load
        self.pv_fn = pv
        self.forecast = forecast or {}          # {UTC hour: kWh}
        self.calls: list[tuple[str, str, dict]] = []
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
        if peak_shaving:        # the Solis peak-shaving mode: grid up to the set power, the battery above it
            s["switch.solis_peak_shaving_mode"] = st("off")
            s["number.solis_peak_max_usable_grid_power"] = st(5000, "W")
            s["number.solis_peak_baseline_soc"] = st(50, "%")
        if feed_in_priority:    # the Solis feed-in priority: solar surplus is exported, not stored
            s["switch.solis_feed_in_priority"] = st("off")
        if prices is not None:
            s["sensor.current_electricity_market_price"] = st(0.1, "€/kWh", prices=prices)
        if with_helpers:
            s["input_boolean.battery_pilot_control"] = st("off")
            s["input_boolean.battery_pilot_block_negative_export"] = st("off")
            s["input_boolean.battery_pilot_fill_battery_for_car"] = st("off")
        self.s = s
        self.helpers_created: list[str] = []
        self.advance(timedelta(0))

    # ---------------------------------------------------------------- HA client interface
    ping_error: str | None = None        # set to make HA unreachable
    last_error: str | None = None

    async def ping(self):
        self.last_error = self.ping_error
        return self.ping_error is None

    async def config(self):
        return {"time_zone": "Europe/Brussels", "latitude": 51.2, "longitude": 4.4}

    async def states(self):
        return {k: {"entity_id": k, **v} for k, v in self.s.items()}

    async def state(self, eid):
        v = self.s.get(eid)
        if v is not None and eid in self.override:
            v = {**v, "state": self.override[eid]}
        return None if v is None else {"entity_id": eid, **v}

    async def services(self):
        return [{"domain": "notify", "services": {"mobile_app_phone": {}, "persistent_notification": {}}}]

    async def call(self, domain, service, data=None):
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
        elif domain == "automation" and service == "turn_off":
            for e in eid if isinstance(eid, list) else [eid]:
                self.s[e]["state"] = "off"

    async def set_state(self, eid, state, attributes=None):
        self.published[eid] = {"state": str(state), "attributes": attributes or {}}

    async def check_config(self):
        return self.check_ok, "" if self.check_ok else "boom"

    async def history(self, ids, start, end):
        return {i: self.history_data[i] for i in ids if i in self.history_data}

    async def ws(self, *commands):
        out = []
        for c in commands:
            if c["type"] == "input_boolean/list":
                out.append([])
            elif c["type"] == "input_boolean/create":
                eid = "input_boolean." + c["name"].lower().replace(" ", "_")
                self.s[eid] = st("off")
                self.helpers_created.append(eid)
                out.append({"id": eid})
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
        return start != end and start <= hhmmss < end

    def mode(self):
        local = self.now.astimezone(TZ)
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
        if mode == "charge":
            ch = min(float(self.s["number.solis_time_charging_charge_current"]["state"]) * V / 1000, room / h if h else 9e9)
            ch = max(ch, min(-net, 2.56)) if net < 0 else ch
            batt = ch
            grid = net + ch / 0.97
        elif mode == "discharge":
            dis = min(float(self.s["number.solis_time_charging_discharge_current"]["state"]) * V / 1000,
                      avail / h if h else 9e9)
            batt = -dis
            grid = net - dis * 0.97
        elif mode == "peak_shave" and net > 0:
            limit = float(self.s["number.solis_peak_max_usable_grid_power"]["state"]) / 1000
            dis = min(max(net - limit, 0) / 0.97, 4.6, avail / h if h else 9e9)
            batt, grid = -dis, net - dis * 0.97
            if self.soc < float(self.s["number.solis_peak_baseline_soc"]["state"]):   # refilled from the grid
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
        self.import_kw = max(grid, 0)
        self.now += dt
        self.s["sensor.solis_grid_import_power"]["state"] = str(round(max(grid, 0) * 1000))
        self.s["sensor.solis_grid_export_power"]["state"] = str(round(max(-grid, 0) * 1000))
        self.s["sensor.solis_household_load_power"]["state"] = str(round(self.load_fn(self.now) * 1000))
        self.s["sensor.solis_total_pv_power"]["state"] = str(round(self.pv_fn(self.now) * 1000))
        self.s["sensor.solis_battery_soc"]["state"] = str(round(self.soc, 1))

    def solis_calls(self):
        return [c for c in self.calls if c[0] in ("switch", "number", "time")]


def utc(*a):
    return datetime(*a, tzinfo=TZ).astimezone(timezone.utc)
