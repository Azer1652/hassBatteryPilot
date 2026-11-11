"""The battery's round-trip efficiency as measured, for display next to the setting.

From the stored quarter-hours: what went into or came out of the battery on the AC side
is load + export - import - solar, and the SOC at the start of this and of the next
quarter gives the change in the battery. AC kWh per SOC point while charging and while
discharging give the round trip (out / in); the battery capacity cancels out.

The SOC comes in whole points, so one quarter says little: the sums over many do.
Quarters with gaps in the data, or with the SOC near empty or full (where a Solis
corrects its SOC), are left out. Old measurements fade with a half-life of 60 days.
The plan does not use this value: it is shown so the setting can be checked.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from .meter import CHANNELS, QuarterRecord

NEEDED_POINTS = 150          # SOC points of charging and of discharging before a value is given
HALF_LIFE_DAYS = 60
SOC_RANGE = (15.0, 95.0)
MIN_FLOW_KWH = 0.02          # a quarter with less than this through the battery is idle
KINDS = ("grid", "solar", "out")


@dataclass
class EfficiencyMeter:
    kwh: dict = field(default_factory=lambda: {k: 0.0 for k in KINDS})      # AC kWh
    points: dict = field(default_factory=lambda: {k: 0.0 for k in KINDS})   # SOC points moved

    def learn_records(self, records: list[QuarterRecord]) -> int:
        """Add one day of quarters; returns how many were used."""
        fade = 0.5 ** (1 / HALF_LIFE_DAYS)
        for k in KINDS:
            self.kwh[k] *= fade
            self.points[k] *= fade
        recs = sorted(records, key=lambda r: r.start)
        n = 0
        for r, nxt in zip(recs, recs[1:]):
            if nxt.start - r.start != timedelta(minutes=15) or r.soc is None or nxt.soc is None:
                continue
            if any(r.covered.get(c, 0) < 0.95 for c in CHANNELS) or r.blocked:
                continue
            if not all(SOC_RANGE[0] <= s <= SOC_RANGE[1] for s in (r.soc, nxt.soc)):
                continue
            out = r.kwh["load"] + r.kwh["export"] - r.kwh["import"] - r.kwh["pv"]
            if abs(out) < MIN_FLOW_KWH:
                continue
            kind = "out" if out > 0 else ("grid" if r.kwh["pv"] < 0.01 else "solar")
            self.kwh[kind] += abs(out)
            self.points[kind] += (r.soc - nxt.soc) if out > 0 else (nxt.soc - r.soc)
            n += 1
        return n

    def _round_trip(self, kinds: tuple) -> float | None:
        kwh_in, pts_in = sum(self.kwh[k] for k in kinds), sum(self.points[k] for k in kinds)
        if pts_in < NEEDED_POINTS or self.points["out"] < NEEDED_POINTS or kwh_in <= 0:
            return None
        pct = 100 * (self.kwh["out"] / self.points["out"]) / (kwh_in / pts_in)
        return round(pct, 1) if 40 <= pct <= 110 else None    # outside: the measurements do not fit

    def report(self, configured_pct: int) -> dict:
        return {"round_trip_pct": self._round_trip(("grid", "solar")), "configured_pct": configured_pct,
                "charge_points": round(self.points["grid"] + self.points["solar"]),
                "discharge_points": round(self.points["out"]), "needed_points": NEEDED_POINTS,
                "grid_round_trip_pct": self._round_trip(("grid",)),
                "solar_round_trip_pct": self._round_trip(("solar",))}

    def to_json(self) -> dict:
        return {"kwh": {k: round(v, 4) for k, v in self.kwh.items()},
                "points": {k: round(v, 3) for k, v in self.points.items()}}

    @classmethod
    def from_json(cls, d: dict | None) -> EfficiencyMeter:
        m = cls()
        for name in ("kwh", "points"):
            for k in KINDS:
                try:
                    getattr(m, name)[k] = float(((d or {}).get(name) or {}).get(k, 0.0))
                except (TypeError, ValueError, AttributeError):
                    pass
        return m
