"""The battery's round-trip efficiency as measured, for display next to the setting.

From the stored quarter-hours: what went into or came out of the battery on the AC side
is load + export - import - solar, and the SOC at the start of this and of the next
quarter gives the change in the battery. AC kWh per SOC point while charging and while
discharging give the round trip (out / in); the battery capacity cancels out.

That AC flow also holds the inverter's own use, which it draws all day whether the
battery moves or not, so it is not a cost of cycling. With the battery power sensor
(DC, + discharging) it is measured in quarters with no solar and the battery idle,
where import - load - export is the inverter's draw. With the inverter's grid-port
power sensor (Solis AC Grid Port Power, + to the grid) the same quarters also give it
directly: what flows into the grid port. The draw shown is the grid port's when there
is enough of it, else the meters'; both are reported. The round trip is then given
without it (the draw taken off what went in and added to what came out, per hour of
charging and discharging; the meters' figure when there is one, since the flow comes
from the same meters) and with it.

The SOC comes in whole points, so one quarter says little: the sums over many do.
Quarters with gaps in the data, or with the SOC near empty or full (where a Solis
corrects its SOC), are left out. Old measurements fade with a half-life of 60 days.
The plan does not use this value: it is shown so the setting can be checked.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from .meter import QuarterRecord

REQUIRED = ("import", "export", "load", "pv")
PORT = "grid_port"           # the inverter's AC grid port, + to the grid (meter channel, optional)
NEEDED_POINTS = 150          # SOC points of charging and of discharging before a value is given
SPLIT_POINTS = 50            # SOC points of grid or of solar charging before that figure is given
HALF_LIFE_DAYS = 60
SOC_RANGE = (15.0, 95.0)
MIN_FLOW_KWH = 0.02          # a quarter with less than this through the battery is idle
KINDS = ("grid", "solar", "out")
IDLE_KWH = 0.005             # battery and solar below this in a quarter (20 W): the inverter's draw shows
DRAW_RANGE_KWH = (-0.0125, 0.05)     # -50 to 200 W: outside it, the sensors do not agree (left out)
DRAW_HOURS = 8.0             # idle hours before the draw is given
Q_H = 0.25


@dataclass
class EfficiencyMeter:
    kwh: dict = field(default_factory=lambda: {k: 0.0 for k in KINDS})      # AC kWh
    points: dict = field(default_factory=lambda: {k: 0.0 for k in KINDS})   # SOC points moved
    hours: dict = field(default_factory=lambda: {k: 0.0 for k in KINDS})    # hours of quarters counted
    draw_kwh: float = 0.0        # the inverter's own use in idle quarters: import - load - export
    draw_hours: float = 0.0
    port_kwh: float = 0.0        # the same quarters, what flowed into the inverter's grid port
    port_hours: float = 0.0
    since: str | None = None     # ISO date of the first day measured

    def learn_records(self, records: list[QuarterRecord]) -> int:
        """Add one day of quarters; returns how many were used for the round trip."""
        fade = 0.5 ** (1 / HALF_LIFE_DAYS)
        for d in (self.kwh, self.points, self.hours):
            for k in KINDS:
                d[k] *= fade
        self.draw_kwh *= fade
        self.draw_hours *= fade
        self.port_kwh *= fade
        self.port_hours *= fade
        recs = sorted(records, key=lambda r: r.start)
        if recs and self.since is None:
            self.since = recs[0].start.date().isoformat()
        for r in recs:
            self._learn_draw(r)
        n = 0
        for r, nxt in zip(recs, recs[1:]):
            if nxt.start - r.start != timedelta(minutes=15) or r.soc is None or nxt.soc is None:
                continue
            if any(r.covered.get(c, 0) < 0.95 for c in REQUIRED) or r.blocked:
                continue
            if not all(SOC_RANGE[0] <= s <= SOC_RANGE[1] for s in (r.soc, nxt.soc)):
                continue
            out = r.kwh["load"] + r.kwh["export"] - r.kwh["import"] - r.kwh["pv"]
            if abs(out) < MIN_FLOW_KWH:
                continue
            kind = "out" if out > 0 else ("grid" if r.kwh["pv"] < 0.01 else "solar")
            self.kwh[kind] += abs(out)
            self.points[kind] += (r.soc - nxt.soc) if out > 0 else (nxt.soc - r.soc)
            self.hours[kind] += Q_H
            n += 1
        return n

    def _learn_draw(self, r: QuarterRecord) -> None:
        """A quarter without solar and with the battery idle: what the grid gives beyond the house
        is the inverter's own use, and so is what flows into its grid port."""
        if any(r.covered.get(c, 0) < 0.95 for c in ("pv", "battery")) or r.blocked:
            return
        if abs(r.kwh.get("battery", 0.0)) >= IDLE_KWH or r.kwh["pv"] >= IDLE_KWH:
            return
        if all(r.covered.get(c, 0) >= 0.95 for c in REQUIRED):
            d = r.kwh["import"] - r.kwh["load"] - r.kwh["export"]
            if DRAW_RANGE_KWH[0] <= d <= DRAW_RANGE_KWH[1]:
                self.draw_kwh += d
                self.draw_hours += Q_H
        if r.covered.get(PORT, 0) >= 0.95:
            d = -r.kwh.get(PORT, 0.0)
            if DRAW_RANGE_KWH[0] <= d <= DRAW_RANGE_KWH[1]:
                self.port_kwh += d
                self.port_hours += Q_H

    def meters_kw(self) -> float | None:
        """The draw as the grid meter and the house load give it."""
        return max(self.draw_kwh / self.draw_hours, 0.0) if self.draw_hours >= DRAW_HOURS else None

    def port_kw(self) -> float | None:
        """The draw as the inverter's grid port gives it."""
        return max(self.port_kwh / self.port_hours, 0.0) if self.port_hours >= DRAW_HOURS else None

    def draw_kw(self) -> float | None:
        """The inverter's own use: its grid port's figure when there is one (measured where it is
        drawn), else the meters'."""
        port = self.port_kw()
        return port if port is not None else self.meters_kw()

    def _round_trip(self, kinds: tuple, need: float, draw_kw: float = 0.0) -> float | None:
        pts_in = sum(self.points[k] for k in kinds)
        kwh_in = sum(self.kwh[k] for k in kinds) - draw_kw * sum(self.hours[k] for k in kinds)
        kwh_out = self.kwh["out"] + draw_kw * self.hours["out"]
        if pts_in < need or self.points["out"] < need or kwh_in <= 0:
            return None
        pct = 100 * (kwh_out / self.points["out"]) / (kwh_in / pts_in)
        return round(pct, 1) if 40 <= pct <= 110 else None    # outside: the measurements do not fit

    def report(self, configured_pct: int) -> dict:
        draw, meters, port = self.draw_kw(), self.meters_kw(), self.port_kw()
        # what goes into the round trip: the meters' draw when there is one (the flow comes from the
        # same meters, so a bias they share cancels), else the grid port's
        rt = meters if meters is not None else port
        both = ("grid", "solar")
        with_draw = self._round_trip(both, NEEDED_POINTS)
        d = rt or 0.0

        def w(kw):
            return None if kw is None else round(kw * 1000)
        return {"round_trip_pct": self._round_trip(both, NEEDED_POINTS, d) if rt is not None else with_draw,
                "round_trip_with_draw_pct": with_draw, "configured_pct": configured_pct,
                "inverter_draw_w": w(draw),
                "inverter_kwh_day": None if draw is None else round(draw * 24, 2),
                "draw_source": None if draw is None else ("grid port" if port is not None else "meters"),
                "draw_meters_w": w(meters), "draw_grid_port_w": w(port),
                "draw_hours": round(max(self.draw_hours, self.port_hours), 1), "needed_draw_hours": DRAW_HOURS,
                "charge_points": round(self.points["grid"] + self.points["solar"]),
                "discharge_points": round(self.points["out"]), "needed_points": NEEDED_POINTS,
                "grid_points": round(self.points["grid"]), "solar_points": round(self.points["solar"]),
                "split_points": SPLIT_POINTS, "since": self.since,
                "grid_round_trip_pct": self._round_trip(("grid",), SPLIT_POINTS, d),
                "solar_round_trip_pct": self._round_trip(("solar",), SPLIT_POINTS, d)}

    def to_json(self) -> dict:
        return {"kwh": {k: round(v, 4) for k, v in self.kwh.items()},
                "points": {k: round(v, 3) for k, v in self.points.items()},
                "hours": {k: round(v, 3) for k, v in self.hours.items()},
                "draw": {"kwh": round(self.draw_kwh, 4), "hours": round(self.draw_hours, 3)},
                "port": {"kwh": round(self.port_kwh, 4), "hours": round(self.port_hours, 3)},
                "since": self.since}

    @classmethod
    def from_json(cls, d: dict | None) -> EfficiencyMeter:
        """Saved state. State from before the hours were counted cannot have the inverter's draw
        taken off, so the measurement starts over."""
        m = cls()
        if not isinstance(d, dict) or not isinstance(d.get("hours"), dict):
            return m
        for name in ("kwh", "points", "hours"):
            for k in KINDS:
                try:
                    getattr(m, name)[k] = float((d.get(name) or {}).get(k, 0.0))
                except (TypeError, ValueError, AttributeError):
                    pass
        try:
            m.draw_kwh = float((d.get("draw") or {}).get("kwh", 0.0))
            m.draw_hours = float((d.get("draw") or {}).get("hours", 0.0))
        except (TypeError, ValueError, AttributeError):
            m.draw_kwh = m.draw_hours = 0.0
        try:
            m.port_kwh = float((d.get("port") or {}).get("kwh", 0.0))
            m.port_hours = float((d.get("port") or {}).get("hours", 0.0))
        except (TypeError, ValueError, AttributeError):
            m.port_kwh = m.port_hours = 0.0
        m.since = d.get("since") if isinstance(d.get("since"), str) else None
        return m
