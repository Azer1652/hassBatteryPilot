"""Commands to a Solis hybrid through the Solis Modbus integration.

The same sequences as the old `script.ps_inverter`, which were tested on an
S6-EH1P: time-of-use slot 1 is set to the current clock hour only, so a
charge or discharge stops by itself within the hour if the app or HA stops.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger(__name__)

NEEDED = ("tou_switch", "grid_charge_switch", "charge_current", "discharge_current",
          "charge_start", "charge_end", "discharge_start", "discharge_end")


def hour_slot(t_local: datetime) -> tuple[str, str]:
    start = t_local.strftime("%H:00:00")
    end = "23:59:00" if t_local.hour == 23 else f"{t_local.hour + 1:02d}:00:00"
    return start, end


@dataclass
class Solis:
    ha: object                      # HA client
    entities: dict                  # key -> entity_id
    mode: str = "unknown"           # what we last told it: self_use | charge | discharge
    slot_hour: str = ""             # local hour the slot was set for
    amps: int = 0
    log: list = field(default_factory=list)   # recent commands for the dashboard

    async def _call(self, domain, service, key, **data):
        await self.ha.call(domain, service, {"entity_id": self.entities[key], **data})

    async def _slot(self, kind: str, start: str, end: str):
        await self._call("time", "set_value", f"{kind}_start", time=start)
        await self._call("time", "set_value", f"{kind}_end", time=end)

    def _note(self, t: datetime, text: str):
        self.log.append({"t": t.isoformat(), "command": text})
        del self.log[:-50]
        log.info("Inverter: %s", text)

    async def charge(self, t_local: datetime, amps: int):
        start, end = hour_slot(t_local)
        await self._slot("discharge", "00:00:00", "00:00:00")
        await self._slot("charge", start, end)
        await self._call("switch", "turn_on", "grid_charge_switch")
        await self._call("number", "set_value", "charge_current", value=amps)
        await self._call("switch", "turn_on", "tou_switch")
        self.mode, self.slot_hour, self.amps = "charge", start, amps
        self._note(t_local, f"charge {amps} A {start[:5]}-{end[:5]}")

    async def discharge(self, t_local: datetime, amps: int):
        start, end = hour_slot(t_local)
        await self._slot("charge", "00:00:00", "00:00:00")
        await self._slot("discharge", start, end)
        await self._call("number", "set_value", "discharge_current", value=amps)
        await self._call("switch", "turn_on", "tou_switch")
        self.mode, self.slot_hour, self.amps = "discharge", start, amps
        self._note(t_local, f"discharge {amps} A {start[:5]}-{end[:5]}")

    async def set_current(self, t_local: datetime, amps: int):
        key = "charge_current" if self.mode == "charge" else "discharge_current"
        await self._call("number", "set_value", key, value=amps)
        self.amps = amps
        self._note(t_local, f"{self.mode} current {amps} A")

    async def self_use(self, t_local: datetime, why: str = ""):
        await self._call("switch", "turn_off", "tou_switch")
        self.mode, self.slot_hour, self.amps = "self_use", "", 0
        self._note(t_local, "self-use" + (f" ({why})" if why else ""))

    async def set_export_limit(self, t_local: datetime, watts: int):
        await self._call("number", "set_value", "export_limit", value=watts)
        self._note(t_local, f"export limit {watts} W")
