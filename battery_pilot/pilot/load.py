"""House consumption profile: average kW for each weekday x hour of the day."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .meter import QuarterRecord

ALPHA = 0.3          # weight of a new day for that weekday-hour (about 3 weeks of memory)
DEFAULT_KW = 0.55    # 13 kWh a day until something is learned


class LoadProfile:
    def __init__(self, kw=None, default_kw: float = DEFAULT_KW):
        self.kw: list[list[float | None]] = kw or [[None] * 24 for _ in range(7)]
        self.default_kw = default_kw

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
            self.learn(local.weekday(), local.hour, sum(r.kwh["load"] for r in recs))
            n += 1
        return n

    def learn_hourly(self, hourly: dict[datetime, tuple[float, float]], tz: ZoneInfo) -> int:
        """Bootstrap from recorder history: {UTC hour: (kWh, coverage)}."""
        n = 0
        for hour, (kwh, cov) in sorted(hourly.items()):
            if cov >= 0.9:
                local = hour.astimezone(tz)
                self.learn(local.weekday(), local.hour, kwh)
                n += 1
        return n

    def to_json(self):
        return {"kw": [[None if v is None else round(v, 4) for v in row] for row in self.kw]}

    @classmethod
    def from_json(cls, d):
        return cls(d["kw"]) if d and d.get("kw") else cls()


def utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)
