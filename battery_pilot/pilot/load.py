"""House consumption profile for each weekday x hour of the day.

`kw` is the average load (for energy), `peak` the typical highest quarter-hour
in that hour (cooking, kettle, dishwasher): the capacity tariff bills
quarter-hours, so the planner keeps enough battery for those peaks.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .meter import QuarterRecord

ALPHA = 0.3          # weight of a new day for that weekday-hour (about 3 weeks of memory)
DEFAULT_KW = 0.55    # 13 kWh a day until something is learned


class LoadProfile:
    def __init__(self, kw=None, default_kw: float = DEFAULT_KW, peak=None):
        self.kw: list[list[float | None]] = kw or [[None] * 24 for _ in range(7)]
        self.peak: list[list[float | None]] = peak or [[None] * 24 for _ in range(7)]
        self.default_kw = default_kw

    def learn_peak(self, weekday: int, hour: int, kw_max: float) -> None:
        old = self.peak[weekday][hour]
        self.peak[weekday][hour] = kw_max if old is None else (1 - ALPHA) * old + ALPHA * kw_max

    def peak_at(self, weekday: int, hour: int) -> float:
        v = self.peak[weekday][hour]
        if v is None:
            same_hour = [row[hour] for row in self.peak if row[hour] is not None]
            v = sum(same_hour) / len(same_hour) if same_hour else None
        return max(v if v is not None else 0.0, self.kw_at(weekday, hour))

    def peak_kw(self, t0: datetime, t1: datetime, tz: ZoneInfo) -> float:
        """Typical highest quarter-hour load (kW) in the hours between t0 and t1."""
        out, t = 0.0, t0
        while t < t1:
            local = t.astimezone(tz)
            out = max(out, self.peak_at(local.weekday(), local.hour))
            t = t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return out

    def learn(self, weekday: int, hour: int, kwh_in_hour: float) -> None:
        old = self.kw[weekday][hour]
        self.kw[weekday][hour] = kwh_in_hour if old is None else (1 - ALPHA) * old + ALPHA * kwh_in_hour

    def kw_at(self, weekday: int, hour: int) -> float:
        v = self.kw[weekday][hour]
        if v is not None:
            return v
        same_hour = [row[hour] for row in self.kw if row[hour] is not None]
        if same_hour:
            return sum(same_hour) / len(same_hour)
        return self.default_kw

    def kwh(self, t0: datetime, t1: datetime, tz: ZoneInfo) -> float:
        total = 0.0
        t = t0
        while t < t1:
            local = t.astimezone(tz)
            nxt = min((t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)), t1)
            total += self.kw_at(local.weekday(), local.hour) * (nxt - t).total_seconds() / 3600
            t = nxt
        return total

    def daily_kwh(self) -> float:
        return round(sum(self.kw_at(d, h) for d in range(7) for h in range(24)) / 7, 2)

    def learned(self) -> bool:
        return any(v is not None for row in self.kw for v in row)

    def learn_records(self, records: list[QuarterRecord], tz: ZoneInfo) -> int:
        by_hour: dict[datetime, list[QuarterRecord]] = {}
        for r in records:
            by_hour.setdefault(r.start.replace(minute=0), []).append(r)
        n = 0
        for hour, recs in sorted(by_hour.items()):
            if len(recs) < 4 or any(r.covered.get("load", 0) < 0.9 for r in recs):
                continue
            local = hour.astimezone(tz)
            # The car is not the house: its energy is taken out of the consumption and of the peaks.
            self.learn(local.weekday(), local.hour, sum(r.house_kwh for r in recs))
            self.learn_peak(local.weekday(), local.hour, max(r.house_kwh for r in recs) * 4)
            n += 1
        return n

    def learn_quarters(self, quarters: dict[datetime, tuple[float, float]], tz: ZoneInfo) -> int:
        """Bootstrap average and peak from recorder history in quarter-hours: {UTC quarter: (kWh, coverage)}."""
        hours: dict[datetime, list[tuple[float, float]]] = {}
        for q, v in quarters.items():
            hours.setdefault(q.replace(minute=0), []).append(v)
        n = 0
        for hour, qs in sorted(hours.items()):
            if len(qs) < 4 or any(cov < 0.9 for _, cov in qs):
                continue
            local = hour.astimezone(tz)
            self.learn(local.weekday(), local.hour, sum(k for k, _ in qs))
            self.learn_peak(local.weekday(), local.hour, max(k for k, _ in qs) * 4)
            n += 1
        return n

    def to_json(self):
        r = lambda m: [[None if v is None else round(v, 4) for v in row] for row in m]
        return {"kw": r(self.kw), "peak": r(self.peak)}

    @classmethod
    def from_json(cls, d):
        return cls(d["kw"], peak=d.get("peak")) if d and d.get("kw") else cls()

