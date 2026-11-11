"""Quarter-hour energy meter and the monthly capacity peak.

The app samples the power sensors every few seconds. `QuarterMeter` integrates
them per quarter-hour (the period the capacity tariff bills) and closes each
quarter into a record that the peak tracker, the shadow bill and the daily
replay use. A gap of more than `MAX_GAP` between samples (app or HA down) is
not filled in; the record says how much of the quarter was covered.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

CHANNELS = ("import", "export", "load", "pv")
MAX_GAP = timedelta(minutes=3)
QUARTER = timedelta(minutes=15)


def quarter_start(t: datetime) -> datetime:
    return t.replace(minute=(t.minute // 15) * 15, second=0, microsecond=0)


@dataclass
class QuarterRecord:
    start: datetime                       # UTC
    kwh: dict                             # channel -> kWh
    covered: dict                         # channel -> fraction of the quarter with data
    soc: float | None = None              # SOC at the start of the quarter
    mode: str | None = None               # inverter mode most of the quarter (shadow: control off)
    blocked: bool = False                 # export was blocked (negative sell price): the panels were held back

    def to_json(self):
        d = {"start": self.start.isoformat(), "kwh": {k: round(v, 5) for k, v in self.kwh.items()},
             "covered": {k: round(v, 3) for k, v in self.covered.items()}, "soc": self.soc}
        if self.mode:
            d["mode"] = self.mode
        if self.blocked:
            d["blocked"] = True
        return d

    @classmethod
    def from_json(cls, d):
        return cls(datetime.fromisoformat(d["start"]), d["kwh"], d["covered"], d.get("soc"), d.get("mode"),
                   bool(d.get("blocked")))


@dataclass
class QuarterMeter:
    current: datetime | None = None
    kwh: dict = field(default_factory=lambda: {c: 0.0 for c in CHANNELS})
    seconds: dict = field(default_factory=lambda: {c: 0.0 for c in CHANNELS})
    last_t: datetime | None = None
    last_kw: dict = field(default_factory=dict)
    soc_at_start: float | None = None

    def add(self, t: datetime, kw: dict, soc: float | None = None) -> list[QuarterRecord]:
        """Add a sample (kW per channel, None = unavailable). Returns quarters that closed."""
        closed: list[QuarterRecord] = []
        if self.current is None:
            self.current, self.soc_at_start = quarter_start(t), soc
        elif self.last_t is not None and t > self.last_t:
            if t - self.last_t > MAX_GAP:
                # App or HA was down: close what we have, nothing is filled in.
                if quarter_start(t) != self.current:
                    closed.append(self._close(soc))
                    self.current = quarter_start(t)
            else:
                seg_start = self.last_t
                while seg_start < t:
                    q_end = self.current + QUARTER
                    seg_end = min(t, q_end)
                    h = (seg_end - seg_start).total_seconds()
                    for c, v in self.last_kw.items():
                        if v is not None:
                            self.kwh[c] += v * h / 3600
                            self.seconds[c] += h
                    if seg_end >= q_end:
                        closed.append(self._close(soc))
                        self.current = q_end
                    seg_start = seg_end
        self.last_t, self.last_kw = t, dict(kw)
        if soc is not None and self.soc_at_start is None:
            self.soc_at_start = soc
        return closed

    def _close(self, next_soc) -> QuarterRecord:
        rec = QuarterRecord(self.current, dict(self.kwh),
                            {c: min(self.seconds[c] / QUARTER.total_seconds(), 1.0) for c in CHANNELS},
                            self.soc_at_start)
        self.kwh = {c: 0.0 for c in CHANNELS}
        self.seconds = {c: 0.0 for c in CHANNELS}
        self.soc_at_start = next_soc
        return rec

    def energy_so_far(self, channel: str) -> float:
        return self.kwh.get(channel, 0.0)

    def projection_kw(self, t: datetime, import_kw: float) -> float:
        """Average import this quarter will end at if the current import continues."""
        if self.current is None:
            return import_kw
        e = self.kwh["import"]
        if self.last_t is not None and self.last_kw.get("import") is not None and t > self.last_t:
            e += self.last_kw["import"] * (t - self.last_t).total_seconds() / 3600
        remain_h = max((self.current + QUARTER - t).total_seconds(), 0) / 3600
        return (e + import_kw * remain_h) * 4


@dataclass
class PeakTracker:
    """Highest quarter-hour average import per month (keyed by local year-month)."""
    months: dict = field(default_factory=dict)       # "2026-11" -> kW
    last_quarter_kw: float = 0.0

    def add(self, rec: QuarterRecord, tz: ZoneInfo) -> None:
        if rec.covered.get("import", 0) < 0.5:
            return
        kw = rec.kwh["import"] * 4
        self.last_quarter_kw = kw
        key = rec.start.astimezone(tz).strftime("%Y-%m")
        self.months[key] = round(max(self.months.get(key, 0.0), kw), 3)
        for k in sorted(self.months)[:-13]:
            del self.months[k]

    def month_peak(self, t: datetime, tz: ZoneInfo) -> float:
        return self.months.get(t.astimezone(tz).strftime("%Y-%m"), 0.0)

    def rolling_average(self, minimum: float) -> float:
        """Average billed peak of the last 12 months (how Fluvius bills the capacity tariff)."""
        vals = [max(v, minimum) for _, v in sorted(self.months.items())[-12:]]
        return round(sum(vals) / len(vals), 3) if vals else minimum

    def to_json(self):
        return {"months": self.months, "last_quarter_kw": self.last_quarter_kw}

    @classmethod
    def from_json(cls, d):
        return cls(dict((d or {}).get("months", {})), (d or {}).get("last_quarter_kw", 0.0))


def history_to_hourly(samples: list[tuple[datetime, str]], scale: float, start: datetime,
                      end: datetime, minutes: int = 60) -> dict[datetime, tuple[float, float]]:
    """Recorder history of a power sensor -> {UTC bucket start: (kWh, fraction of the bucket with data)}.

    Buckets are hours, or quarter-hours with `minutes=15`.

    `samples` are (time, state) pairs in time order; a state holds until the next one;
    'unavailable', 'unknown' and other non-numbers are gaps. `scale` turns the state into kW.
    """
    out: dict[datetime, list[float]] = {}
    for i, (t, raw) in enumerate(samples):
        try:
            kw = float(raw) * scale
        except (TypeError, ValueError):
            continue
        seg_start = max(t, start)
        seg_end = min(samples[i + 1][0] if i + 1 < len(samples) else end, end)
        while seg_start < seg_end:
            bucket = seg_start.replace(minute=(seg_start.minute // minutes) * minutes if minutes < 60 else 0,
                                       second=0, microsecond=0)
            stop = min(bucket + timedelta(minutes=minutes), seg_end)
            sec = (stop - seg_start).total_seconds()
            acc = out.setdefault(bucket, [0.0, 0.0])
            acc[0] += kw * sec / 3600
            acc[1] += sec / (minutes * 60)
            seg_start = stop
    return {h: (v[0], min(v[1], 1.0)) for h, v in out.items()}
