"""Commands to a Solis hybrid through the Solis Modbus integration.

The same sequences as the old `script.ps_inverter`, which were tested on an
S6-EH1P: time-of-use slot 1 is set to the current clock hour only, so a
charge or discharge stops by itself when the hour ends if the app or HA stops.
The Solis repeats its time-of-use slots every day, though: the app records
what it left on the inverter and switches it back to self-use when it starts.

Peak shaving (optional, when the integration has the switch): the inverter's own
mode in which the grid feeds the house up to a set power and the battery only
gives what is above it. Time of use is off then; every other command turns the
peak-shaving switch off again.

Solar hold (optional, when the integration has the feed-in priority switch): solar
surplus goes to the grid and not into the battery. Handled like peak shaving.

Force (`method == "force"`, when the integration has the actions): charge and discharge go
through the Solis Modbus actions `solis_force_battery_charge` / `_discharge` (the inverter's
remote control: mode, power in W and a timeout of at most 30 minutes, after which the
inverter returns to its own mode by itself). The app sends them again every FORCE_RENEW, so
they lapse within FORCE_MINUTES when the app or Home Assistant stops. Time of use is kept off.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger(__name__)

NEEDED = ("tou_switch", "grid_charge_switch", "charge_current", "discharge_current",
          "charge_start", "charge_end", "discharge_start", "discharge_end")
OPTIONAL = ("export_limit", "peak_shaving_switch", "peak_shaving_limit", "peak_shaving_soc",
            "feed_in_priority_switch")
FORCE_DOMAIN = "solis_modbus"
FORCE_MINUTES = 30              # the longest timeout the action accepts
FORCE_RENEW_S = 10 * 60         # sent again this often while it should go on


def hour_slot(t_local: datetime) -> tuple[str, str]:
    start = t_local.strftime("%H:00:00")
    end = "23:59:00" if t_local.hour == 23 else f"{t_local.hour + 1:02d}:00:00"
    return start, end


@dataclass
class Solis:
    ha: object                      # HA client
    entities: dict                  # key -> entity_id
    mode: str = "unknown"           # what we last told it: self_use | charge | discharge | peak_shave | solar_hold,
                                    # unknown (nothing yet) or failed (a command failed half-way)
    slot_hour: str = ""             # local hour the slot was set for
    amps: int = 0
    shave: bool | None = None       # the peak-shaving switch as we last set it (None: not known)
    hold: bool | None = None        # the feed-in priority switch as we last set it (None: not known)
    shave_set: tuple | None = None  # (limit, SOC) last written for peak shaving: written again only when changed
    shave_limit_per_kw: float = 1000.0   # the limit number is in W (1000) or kW (1)
    log: list = field(default_factory=list)   # recent commands for the dashboard
    method: str = "time_slot"       # time_slot | force (see the module text)
    volts: float = 51.2             # battery voltage, to turn amps into the force action's watts
    slave: int = 0                  # Modbus slave ID for the force action (0: not sent)
    forced: bool | None = None      # a force command of ours may be running (None: not known)
    sent_at: datetime | None = None  # when the last force charge or discharge was sent

    async def _call(self, domain, service, key, **data):
        await self.ha.call(domain, service, {"entity_id": self.entities[key], **data})

    async def _maybe(self, domain, service, key, **data):
        if key in self.entities:
            await self._call(domain, service, key, **data)

    @property
    def force(self) -> bool:
        return self.method == "force"

    def watts(self, amps: int) -> int:
        return int(round(amps * self.volts / 100) * 100)

    def renew_due(self, t: datetime) -> bool:
        """A force charge or discharge is running and must be sent again before it lapses."""
        return (self.force and self.mode in ("charge", "discharge")
                and (self.sent_at is None or (t - self.sent_at).total_seconds() >= FORCE_RENEW_S))

    async def _force(self, service: str, amps: int):
        data = {"power_watts": self.watts(amps), "duration_minutes": FORCE_MINUTES}
        if self.slave:
            data["slave"] = self.slave
        await self.ha.call(FORCE_DOMAIN, service, data)
        self.forced = True

    async def _force_off(self):
        """Stop a force command of ours (or one whose state is not known)."""
        if self.force and self.forced is not False:
            data = {"slave": self.slave} if self.slave else {}
            await self.ha.call(FORCE_DOMAIN, "solis_stop_force_charge_discharge", data)
            self.forced = False

    async def _slot(self, kind: str, start: str, end: str):
        await self._call("time", "set_value", f"{kind}_start", time=start)
        await self._call("time", "set_value", f"{kind}_end", time=end)

    def _note(self, t: datetime, text: str):
        self.log.append({"t": t.isoformat(), "command": text})
        del self.log[:-50]
        log.info("Inverter: %s", text)

    async def _shave_off(self):
        if "peak_shaving_switch" in self.entities and self.shave is not False:
            await self._call("switch", "turn_off", "peak_shaving_switch")
            self.shave = False
        if "feed_in_priority_switch" in self.entities and self.hold is not False:
            await self._call("switch", "turn_off", "feed_in_priority_switch")
            self.hold = False

    async def solar_hold(self, t_local: datetime):
        """Solar surplus to the grid, not into the battery (the inverter's feed-in priority)."""
        await self._force_off()
        await self._maybe("switch", "turn_off", "tou_switch")
        if "peak_shaving_switch" in self.entities and self.shave is not False:
            await self._call("switch", "turn_off", "peak_shaving_switch")
            self.shave = False
        await self._call("switch", "turn_on", "feed_in_priority_switch")
        self.mode, self.slot_hour, self.amps, self.hold = "solar_hold", "", 0, True
        self._note(t_local, "solar hold: surplus to the grid")

    async def peak_shave(self, t_local: datetime, limit_kw: float, soc: float):
        """Grid up to `limit_kw`, the battery above it; `soc`: the level the inverter keeps in the battery."""
        await self._force_off()
        await self._maybe("switch", "turn_off", "tou_switch")
        if "feed_in_priority_switch" in self.entities and self.hold is not False:
            await self._call("switch", "turn_off", "feed_in_priority_switch")
            self.hold = False
        limit = round(limit_kw * self.shave_limit_per_kw, 2 if self.shave_limit_per_kw < 10 else 0)
        if self.shave_set != (limit, soc):
            if "peak_shaving_limit" in self.entities:
                await self._call("number", "set_value", "peak_shaving_limit", value=limit)
            if "peak_shaving_soc" in self.entities:
                await self._call("number", "set_value", "peak_shaving_soc", value=soc)
            self.shave_set = (limit, soc)
        await self._call("switch", "turn_on", "peak_shaving_switch")
        self.mode, self.slot_hour, self.amps, self.shave = "peak_shave", "", 0, True
        self._note(t_local, f"peak shaving at {limit_kw:g} kW")

    async def charge(self, t_local: datetime, amps: int):
        start, end = hour_slot(t_local)
        if self.force:
            await self._shave_off()
            await self._maybe("switch", "turn_off", "tou_switch")
            await self._maybe("switch", "turn_on", "grid_charge_switch")
            await self._force("solis_force_battery_charge", amps)
            self.mode, self.slot_hour, self.amps, self.sent_at = "charge", start, amps, t_local
            self._note(t_local, f"force charge {self.watts(amps)} W ({amps} A) for {FORCE_MINUTES} min")
            return
        await self._shave_off()
        await self._slot("discharge", "00:00:00", "00:00:00")
        await self._slot("charge", start, end)
        await self._call("switch", "turn_on", "grid_charge_switch")
        await self._call("number", "set_value", "charge_current", value=amps)
        await self._call("switch", "turn_on", "tou_switch")
        self.mode, self.slot_hour, self.amps = "charge", start, amps
        self._note(t_local, f"charge {amps} A {start[:5]}-{end[:5]}")

    async def discharge(self, t_local: datetime, amps: int):
        start, end = hour_slot(t_local)
        if self.force:
            await self._shave_off()
            await self._maybe("switch", "turn_off", "tou_switch")
            await self._force("solis_force_battery_discharge", amps)
            self.mode, self.slot_hour, self.amps, self.sent_at = "discharge", start, amps, t_local
            self._note(t_local, f"force discharge {self.watts(amps)} W ({amps} A) for {FORCE_MINUTES} min")
            return
        await self._shave_off()
        await self._slot("charge", "00:00:00", "00:00:00")
        await self._slot("discharge", start, end)
        await self._call("number", "set_value", "discharge_current", value=amps)
        await self._call("switch", "turn_on", "tou_switch")
        self.mode, self.slot_hour, self.amps = "discharge", start, amps
        self._note(t_local, f"discharge {amps} A {start[:5]}-{end[:5]}")

    async def set_current(self, t_local: datetime, amps: int):
        if self.force:
            await self._force("solis_force_battery_charge" if self.mode == "charge" else "solis_force_battery_discharge",
                              amps)
            self.amps, self.sent_at = amps, t_local
            self._note(t_local, f"force {self.mode} {self.watts(amps)} W ({amps} A)")
            return
        key = "charge_current" if self.mode == "charge" else "discharge_current"
        await self._call("number", "set_value", key, value=amps)
        self.amps = amps
        self._note(t_local, f"{self.mode} current {amps} A")

    async def self_use(self, t_local: datetime, why: str = ""):
        await self._force_off()
        await self._maybe("switch", "turn_off", "tou_switch")
        await self._shave_off()
        self.mode, self.slot_hour, self.amps = "self_use", "", 0
        self._note(t_local, "self-use" + (f" ({why})" if why else ""))

    async def set_export_limit(self, t_local: datetime, watts: int):
        await self._call("number", "set_value", "export_limit", value=watts)
        self._note(t_local, f"export limit {watts} W")
