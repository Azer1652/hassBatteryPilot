"""A Marstek Venus battery (AC-coupled) instead of a Solis hybrid.

Two Home Assistant integrations can see the same battery, and Battery Pilot uses both:

* **Marstek Local API** (`marstek_local_api`, jaapp/ha-marstek-local-api): the battery's own
  UDP API on the LAN. Readings (SOC, power, voltage) and the only way to steer it:
  `marstek_local_api.set_passive_mode` (power in W, negative = charge, positive = discharge,
  for a duration in seconds) and the *Auto mode* button (the battery's own self-consumption on
  its CT or P1 meter, which is self-use here).
* **Marstek Cloud** (`marstek_cloud`, DoctaShizzle/marstek_cloud): the same battery through
  Marstek's cloud. Readings only (SOC, charge and discharge power, report time).

The local link drops out now and then. The local integration then keeps showing its last values
and only its *Last message received* sensor grows, so a reading counts as fresh only while that
is under `marstek_stale_s`; otherwise the cloud's reading is used, when its report is fresh.
With neither fresh, the SOC is unknown and the app's blind rule returns the battery to self-use.

Commands have no second path (the cloud integration cannot steer), so they are made to fail
safe: a passive charge or discharge runs for PASSIVE_S and is sent again every RENEW_S while it
should go on, so when commands stop arriving the battery leaves passive mode by itself. A
command that fails leaves the mode `failed` and the next tick sends it again (as with a Solis).

The class has the same interface as `solis.Solis`, so the app, the controller and the planner
are unchanged: amps stay the controller's unit and become watts here (amps x battery voltage).
A Marstek has no peak-shaving or feed-in priority switch, so the plan never uses those modes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .solis import hour_slot

log = logging.getLogger(__name__)

LOCAL = "marstek_local_api"
CLOUD = "marstek_cloud"
PASSIVE_SERVICE = "set_passive_mode"
PASSIVE_S = 20 * 60          # a passive charge or discharge lapses by itself after this
RENEW_S = 5 * 60             # and is sent again this often while it should go on

# Entity key -> (integration, end of the entity's unique ID). Found in the entity registry, so
# entity names chosen by the user (or by Marstek's cloud) do not matter.
REGISTRY = {
    "battery_soc": (LOCAL, "battery_soc"),
    "battery_power": (LOCAL, "battery_power"),            # W, + charging
    "battery_voltage": (LOCAL, "battery_voltage"),
    "marstek_last_message": (LOCAL, "last_message_received"),
    "marstek_auto_mode_button": (LOCAL, "auto_mode_button"),
    "battery_soc_backup": (CLOUD, "soc"),
    "battery_charge_power_backup": (CLOUD, "charge"),
    "battery_discharge_power_backup": (CLOUD, "discharge"),
    "battery_report_time_backup": (CLOUD, "report_time"),
}
# Unique IDs that end like one above but are something else.
NOT = {"charge": ("total_charge",)}


def registry_match(key: str, registry: list[dict]) -> list[dict]:
    """Entity registry entries of the Marstek integrations that are `key`."""
    platform, suffix = REGISTRY[key]
    out = []
    for e in registry or []:
        uid = str(e.get("unique_id") or "")
        if e.get("platform") != platform or e.get("disabled_by") or not uid.endswith("_" + suffix):
            continue
        if any(uid.endswith("_" + other) for other in NOT.get(suffix, ())):
            continue
        out.append(e)
    return out


def device_of(entity_id: str | None, registry: list[dict]) -> str | None:
    for e in registry or []:
        if e.get("entity_id") == entity_id:
            return e.get("device_id") or None
    return None


def age_s(state: str | None, now: datetime) -> float | None:
    """How old the cloud's report is, from its report-time sensor: an epoch time (s or ms) or an ISO
    date. None: not known (the reading is then taken as it is)."""
    if state in (None, "", "unknown", "unavailable"):
        return None
    try:
        v = float(state)
    except ValueError:
        try:
            t = datetime.fromisoformat(str(state).replace("Z", "+00:00"))
        except ValueError:
            return None
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max((now - t).total_seconds(), 0.0)
    if v > 1e12:
        v /= 1000
    if v < 1e9:
        return None
    return max(now.timestamp() - v, 0.0)


@dataclass
class Source:
    """One integration's view of the battery."""
    soc: float | None
    kw: float | None             # battery power, + discharging (the app's sign)
    age_s: float | None          # how old the reading is (None: not known)


@dataclass
class Reading:
    soc: float | None
    kw: float | None
    source: str                  # local | cloud | none
    why: str = ""


def choose(local: Source, cloud: Source, stale_s: float) -> Reading:
    """The fresh reading, the local one first."""
    def fresh(s: Source) -> bool:
        return s.soc is not None and (s.age_s is None or s.age_s <= stale_s)

    if fresh(local):
        return Reading(local.soc, local.kw, "local")
    lwhy = ("no reading" if local.soc is None
            else f"no message for {local.age_s:.0f} s")
    if fresh(cloud):
        return Reading(cloud.soc, cloud.kw, "cloud", f"local API: {lwhy}")
    cwhy = "no reading" if cloud.soc is None else f"last report {cloud.age_s:.0f} s ago"
    return Reading(None, None, "none", f"local API: {lwhy}; cloud: {cwhy}")


@dataclass
class Marstek:
    ha: object                      # HA client
    entities: dict                  # key -> entity_id (marstek_auto_mode_button, export_limit)
    device_id: str = ""             # the battery's Home Assistant device, for set_passive_mode
    mode: str = "unknown"           # as solis.Solis.mode (never peak_shave or solar_hold)
    slot_hour: str = ""
    amps: int = 0
    shave: bool | None = False      # (no peak-shaving switch: kept for the app's bookkeeping)
    hold: bool | None = False
    shave_set: tuple | None = None
    shave_limit_per_kw: float = 1000.0
    log: list = field(default_factory=list)
    method: str = "force"           # always remote control: the app treats it like the Solis force action
    volts: float = 51.2
    slave: int = 0
    forced: bool | None = None      # a passive command of ours may be running (None: not known)
    sent_at: datetime | None = None

    @property
    def force(self) -> bool:
        return True

    def watts(self, amps: int) -> int:
        return int(round(amps * self.volts / 100) * 100)

    def renew_due(self, t: datetime) -> bool:
        return (self.mode in ("charge", "discharge")
                and (self.sent_at is None or (t - self.sent_at).total_seconds() >= RENEW_S))

    def _note(self, t: datetime, text: str):
        self.log.append({"t": t.isoformat(), "command": text})
        del self.log[:-50]
        log.info("Marstek: %s", text)

    async def _passive(self, watts: int):
        await self.ha.call(LOCAL, PASSIVE_SERVICE, {"device_id": self.device_id, "power": watts,
                                                    "duration": PASSIVE_S})
        self.forced = True

    async def charge(self, t_local: datetime, amps: int):
        await self._passive(-self.watts(amps))
        self.mode, self.slot_hour, self.amps, self.sent_at = "charge", hour_slot(t_local)[0], amps, t_local
        self._note(t_local, f"passive charge {self.watts(amps)} W ({amps} A) for {PASSIVE_S // 60} min")

    async def discharge(self, t_local: datetime, amps: int):
        await self._passive(self.watts(amps))
        self.mode, self.slot_hour, self.amps, self.sent_at = "discharge", hour_slot(t_local)[0], amps, t_local
        self._note(t_local, f"passive discharge {self.watts(amps)} W ({amps} A) for {PASSIVE_S // 60} min")

    async def set_current(self, t_local: datetime, amps: int):
        w = self.watts(amps)
        await self._passive(-w if self.mode == "charge" else w)
        self.amps, self.sent_at = amps, t_local
        self._note(t_local, f"passive {self.mode} {w} W ({amps} A)")

    async def self_use(self, t_local: datetime, why: str = ""):
        await self.ha.call("button", "press", {"entity_id": self.entities["marstek_auto_mode_button"]})
        self.mode, self.slot_hour, self.amps, self.forced = "self_use", "", 0, False
        self._note(t_local, "auto mode (self-use)" + (f" ({why})" if why else ""))

    async def peak_shave(self, t_local: datetime, limit_kw: float, soc: float):
        raise RuntimeError("a Marstek battery has no peak-shaving mode")

    async def solar_hold(self, t_local: datetime):
        raise RuntimeError("a Marstek battery has no feed-in priority mode")

    async def set_export_limit(self, t_local: datetime, watts: int):
        """The export limit is the solar inverter's (a Solax `export_control_user_limit`, for one)."""
        await self.ha.call("number", "set_value", {"entity_id": self.entities["export_limit"], "value": watts})
        self._note(t_local, f"export limit {watts} W")
