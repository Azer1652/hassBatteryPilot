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
safe: every passive command runs for PASSIVE_S (5 minutes) and is sent again every RENEW_S while
it should go on, so when commands stop arriving the battery leaves passive mode within minutes. A
command that fails or times out (`MarstekTimeout`, CALL_TIMEOUT_S) leaves the mode `failed`, and
the app sends nothing for RETRY_S before trying again: the integration itself retries for up to
45 s, and its README warns that a busier local API can make the battery unresponsive.

The class has the same interface as `solis.Solis`, so the app, the controller and the planner
are unchanged: amps stay the controller's unit and become watts here (amps x battery voltage).

A Marstek has no peak-shaving or feed-in priority mode of its own, so Battery Pilot does them
itself with passive mode, from the grid meter (a HomeWizard P1 meter, for one) at every tick
(`follow`):

* peak shaving - the battery gives only what the house takes above the import limit, never
  below the shaving level, and solar surplus still charges it (what a Solis does, and what the
  planner and the simulator assume);
* solar hold - passive at 0 W: surplus goes to the grid, the battery waits.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo

from .ha import HAUnreachable
from .solis import hour_slot

log = logging.getLogger(__name__)

LOCAL = "marstek_local_api"
CLOUD = "marstek_cloud"
PASSIVE_SERVICE = "set_passive_mode"
PASSIVE_S = 5 * 60           # every passive command lapses by itself after this (a lost link gives the battery back)
RENEW_S = 2 * 60             # and is sent again this often while it should go on
FOLLOW_STEP_W = 200          # peak shaving: a new power is sent when it differs this much
FOLLOW_GAP_S = 60            # and not sooner than this after the last one (the local API is slow)
URGENT_KW = 0.5              # except when the grid is this far over the import limit (the capacity peak)
URGENT_GAP_S = 30            # then after this
RETRY_S = 60                 # after a failed command, nothing is sent for this long
CALL_TIMEOUT_S = 20          # a Marstek action that takes longer than this has failed


class MarstekTimeout(Exception):
    """The Marstek did not answer in time (the local link is down): a command failure, not Home
    Assistant going away."""

# Entity key -> (integration, end of the entity's unique ID). Found in the entity registry, so
# entity names chosen by the user (or by Marstek's cloud) do not matter.
REGISTRY = {
    "battery_soc": (LOCAL, "battery_soc"),
    "battery_power": (LOCAL, "battery_power"),            # W, + charging
    "battery_voltage": (LOCAL, "battery_voltage"),
    "marstek_last_message": (LOCAL, "last_message_received"),
    "marstek_auto_mode_button": (LOCAL, "auto_mode_button"),
    "marstek_mode_select": (LOCAL, "operating_mode", "select"),   # (the sensor of the same name is not it)
    "battery_soc_backup": (CLOUD, "soc"),
    "battery_charge_power_backup": (CLOUD, "charge"),
    "battery_discharge_power_backup": (CLOUD, "discharge"),
    "battery_report_time_backup": (CLOUD, "report_time"),
}


def auto_option(options) -> str:
    """The option of an operating-mode select that is Auto mode (its spelling varies)."""
    for o in options or []:
        if str(o).strip().lower() in ("auto", "auto mode", "automatic"):
            return str(o)
    for o in options or []:
        if "auto" in str(o).lower():
            return str(o)
    return "Auto"


# Unique IDs that end like one above but are something else.
NOT = {"charge": ("total_charge",)}


def registry_match(key: str, registry: list[dict]) -> list[dict]:
    """Entity registry entries of the Marstek integrations that are `key`."""
    platform, suffix, *domain = REGISTRY[key]
    out = []
    for e in registry or []:
        uid = str(e.get("unique_id") or "")
        if e.get("platform") != platform or e.get("disabled_by") or not uid.endswith("_" + suffix):
            continue
        if domain and not str(e.get("entity_id") or "").startswith(domain[0] + "."):
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


def age_s(state: str | None, now: datetime, tz: tzinfo = timezone.utc) -> float | None:
    """How old the cloud's report is, from its report-time sensor: an epoch time (s or ms) or an ISO
    date (without a zone: in Home Assistant's, `tz`). None: not known (the reading is then not used)."""
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
            t = t.replace(tzinfo=tz)
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
    """The fresh reading, the local one first. A reading counts only with a known age: both
    integrations keep showing their last values when their link is down."""
    def fresh(s: Source) -> bool:
        return s.soc is not None and s.age_s is not None and s.age_s <= stale_s

    def why(s: Source, old: str) -> str:
        return "no reading" if s.soc is None else "its age is unknown" if s.age_s is None else old.format(s.age_s)

    if fresh(local):
        return Reading(local.soc, local.kw, "local")
    lwhy = why(local, "no message for {:.0f} s")
    if fresh(cloud):
        return Reading(cloud.soc, cloud.kw, "cloud", f"local API: {lwhy}")
    cwhy = why(cloud, "last report {:.0f} s ago")
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
    max_charge_w: int = 2500
    max_discharge_w: int = 2500
    power_w: int = 0                # peak shaving / solar hold: the passive power now (+ discharge)
    shave_limit_kw: float = 0.0
    shave_soc: float = 0.0
    retry_at: datetime | None = None    # after a failed command: nothing is sent before this
    auto_option: str = "Auto"           # the operating-mode select's option for Auto mode

    def waiting(self, t: datetime) -> bool:
        """A command failed less than RETRY_S ago: the local API gets a rest."""
        return self.retry_at is not None and t < self.retry_at

    def failed(self, t: datetime):
        self.retry_at = t + timedelta(seconds=RETRY_S)

    @property
    def force(self) -> bool:
        return True

    def watts(self, amps: int) -> int:
        return int(round(amps * self.volts / 100) * 100)

    def renew_due(self, t: datetime) -> bool:
        """A charge or discharge must be sent again (peak shaving and solar hold renew in `follow`)."""
        return (self.mode in ("charge", "discharge")
                and (self.sent_at is None or (t - self.sent_at).total_seconds() >= RENEW_S))

    def shave_power(self, grid_kw: float, soc: float) -> int:
        """Peak shaving: the passive power (W, + discharge) that brings the grid (+ import, kW) to the
        import limit when the house asks more, and charges what would be exported. Built on the power
        sent last, since the grid meter sees the battery's effect."""
        p, limit = self.power_w / 1000, self.shave_limit_kw
        if grid_kw > limit or p > 0:
            p = max(p + grid_kw - limit, 0.0)
        elif grid_kw < 0 or p < 0:
            p = min(p + grid_kw, 0.0)
        else:
            p = 0.0
        w = int(round(p * 1000 / 50) * 50)
        if w > 0 and soc <= self.shave_soc:
            w = 0
        if w < 0 and soc >= 100:
            w = 0
        return max(min(w, self.max_discharge_w), -self.max_charge_w)

    async def follow(self, t_local: datetime, grid_kw: float | None, soc: float | None):
        """At every tick, in peak shaving or solar hold: send the passive power again when it should
        change, or before it lapses."""
        if self.mode not in ("peak_shave", "solar_hold"):
            return
        if self.mode == "solar_hold" or grid_kw is None or soc is None:
            w = 0 if self.mode == "solar_hold" else self.power_w
        else:
            w = self.shave_power(grid_kw, soc)
        age = (t_local - self.sent_at).total_seconds() if self.sent_at else 1e9
        urgent = grid_kw is not None and grid_kw > self.shave_limit_kw + URGENT_KW and w > self.power_w
        gap = URGENT_GAP_S if urgent else FOLLOW_GAP_S
        if age >= RENEW_S or (abs(w - self.power_w) >= FOLLOW_STEP_W and age >= gap) \
                or (w == 0 and self.power_w != 0 and age >= gap):
            await self._passive(w)
            changed = w != self.power_w
            self.power_w, self.sent_at = w, t_local
            if changed:
                self._note(t_local, f"{self.mode.replace('_', ' ')}: passive "
                                    + (f"discharge {w} W" if w > 0 else f"charge {-w} W" if w < 0 else "0 W (idle)"))

    def _note(self, t: datetime, text: str):
        self.log.append({"t": t.isoformat(), "command": text})
        del self.log[:-50]
        log.info("Marstek: %s", text)

    async def _call(self, domain: str, service: str, data: dict):
        """A Marstek action; one that times out is the Marstek's failure, not Home Assistant's."""
        try:
            await self.ha.call(domain, service, data, timeout=CALL_TIMEOUT_S)
        except HAUnreachable as e:
            if isinstance(e.__cause__, TimeoutError):
                raise MarstekTimeout(f"the Marstek did not answer within {CALL_TIMEOUT_S} s ({domain}.{service})") from e
            raise
        self.retry_at = None

    async def _passive(self, watts: int):
        watts = max(min(watts, self.max_discharge_w), -self.max_charge_w)
        await self._call(LOCAL, PASSIVE_SERVICE, {"device_id": self.device_id, "power": watts, "duration": PASSIVE_S})
        self.forced = True

    async def charge(self, t_local: datetime, amps: int):
        w = min(self.watts(amps), self.max_charge_w)
        await self._passive(-w)
        self.mode, self.slot_hour, self.amps, self.sent_at = "charge", hour_slot(t_local)[0], amps, t_local
        self.power_w, self.shave, self.hold = -w, False, False
        self._note(t_local, f"passive charge {w} W ({amps} A) for {PASSIVE_S // 60} min")

    async def discharge(self, t_local: datetime, amps: int):
        w = min(self.watts(amps), self.max_discharge_w)
        await self._passive(w)
        self.mode, self.slot_hour, self.amps, self.sent_at = "discharge", hour_slot(t_local)[0], amps, t_local
        self.power_w, self.shave, self.hold = w, False, False
        self._note(t_local, f"passive discharge {w} W ({amps} A) for {PASSIVE_S // 60} min")

    async def set_current(self, t_local: datetime, amps: int):
        w = min(self.watts(amps), self.max_charge_w if self.mode == "charge" else self.max_discharge_w)
        await self._passive(-w if self.mode == "charge" else w)
        self.amps, self.sent_at, self.power_w = amps, t_local, -w if self.mode == "charge" else w
        self._note(t_local, f"passive {self.mode} {w} W ({amps} A)")

    async def peak_shave(self, t_local: datetime, limit_kw: float, soc: float):
        """Battery Pilot's own peak shaving (see the module text): starts idle, `follow` sets the power."""
        await self._passive(0)
        self.mode, self.slot_hour, self.amps, self.sent_at, self.power_w = "peak_shave", "", 0, t_local, 0
        self.shave_limit_kw, self.shave_soc, self.shave_set, self.shave = limit_kw, soc, (limit_kw, soc), True
        self._note(t_local, f"peak shaving at {limit_kw:g} kW (by Battery Pilot, down to {soc:.0f} %)")

    async def solar_hold(self, t_local: datetime):
        """Solar surplus to the grid: passive at 0 W, the battery neither charges nor discharges."""
        await self._passive(0)
        self.mode, self.slot_hour, self.amps, self.sent_at, self.power_w = "solar_hold", "", 0, t_local, 0
        self.shave, self.hold = False, True
        self._note(t_local, "solar hold: passive 0 W, surplus to the grid")

    async def self_use(self, t_local: datetime, why: str = ""):
        """Auto mode: through the operating-mode select when there is one, else the Auto mode button."""
        if "marstek_mode_select" in self.entities:
            await self._call("select", "select_option", {"entity_id": self.entities["marstek_mode_select"],
                                                          "option": self.auto_option})
        else:
            await self._call("button", "press", {"entity_id": self.entities["marstek_auto_mode_button"]})
        self.mode, self.slot_hour, self.amps, self.forced, self.power_w = "self_use", "", 0, False, 0
        self.shave, self.hold = False, False
        self._note(t_local, "auto mode (self-use)" + (f" ({why})" if why else ""))

    async def set_export_limit(self, t_local: datetime, watts: int):
        """The export limit is the solar inverter's (a Solax `export_control_user_limit`, for one)."""
        await self.ha.call("number", "set_value", {"entity_id": self.entities["export_limit"], "value": watts})
        self._note(t_local, f"export limit {watts} W")
