"""Planned versus actual, day by day.

The plan is redone every few minutes from the live SOC, so what it predicted is
kept when it is made, in two forms:

* **day ahead** - the first plan after the nightly learning (about 00:20): the
  SOC it expected for the whole day, with the solar forecast (raw and after
  shade) and the house load it was based on. Its errors add up over the day.
* **hour by hour** - for every clock hour, the plan in force at its start: the
  SOC it expected by the end of that hour and the forecast for that hour. Each
  one starts again from the measured SOC.

Once a night the day is closed: those predictions and the measured quarter-hours
(SOC, solar, house, grid, inverter mode) go into one file per day under
`/data/history`. Times in a day are minutes from local midnight, counted in real
time, so 23- and 25-hour days (DST) work.
"""
from __future__ import annotations

import json
import os
import re
import statistics
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .meter import QUARTER, QuarterRecord
from .planner import Plan
from .solar import PvForecast

HISTORY_DAYS = 60       # days kept in /data/history
OPEN_DAYS = 3           # days of predictions kept in the state until their day is closed
COMPLETE = 0.9          # share of the day's quarters needed to call the day complete
FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")

LoadFn = Callable[[datetime, datetime], float]


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Local midnight to midnight, in UTC."""
    start = datetime.combine(day, time(0), tz).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time(0), tz).astimezone(timezone.utc)
    return start, end


def minutes(t: datetime, origin: datetime) -> int:
    return int(round((t - origin).total_seconds() / 60))


# ---------------------------------------------------------------- predictions
def plan_points(plan: Plan, t0: datetime, t1: datetime, origin: datetime) -> list[list]:
    """The plan's SOC at its step boundaries between t0 and t1: [[minute, soc], ...]."""
    pts: list[list] = []
    for s in plan.steps:
        a, b = s.start, s.start + timedelta(hours=s.hours)
        if b <= t0 or a >= t1:
            continue
        lo, hi = max(a, t0), min(b, t1)

        def soc_at(t, s=s, a=a, b=b):
            return s.soc_start + (s.soc_end - s.soc_start) * (t - a) / (b - a)

        first = [minutes(lo, origin), round(soc_at(lo), 1)]
        if not pts or pts[-1][0] != first[0]:
            pts.append(first)
        last = [minutes(hi, origin), round(soc_at(hi), 1)]
        if last[0] != pts[-1][0]:
            pts.append(last)
    return pts


def plan_actions(plan: Plan, t0: datetime, t1: datetime, origin: datetime) -> list[list]:
    """The plan's actions between t0 and t1 as runs: [[from minute, to minute, action], ...]."""
    runs: list[list] = []
    for s in plan.steps:
        a, b = max(s.start, t0), min(s.start + timedelta(hours=s.hours), t1)
        if b <= a:
            continue
        m0, m1 = minutes(a, origin), minutes(b, origin)
        if runs and runs[-1][2] == s.action and runs[-1][1] == m0:
            runs[-1][1] = m1
        elif m1 > m0:
            runs.append([m0, m1, s.action])
    return runs


def forecast_hours(pv: PvForecast, load_kwh: LoadFn, start: datetime, end: datetime) -> list[list]:
    """[[minute, solar forecast kWh, after shade kWh, house kWh], ...] per hour from start to end."""
    out = []
    t = start
    while t < end:
        t2 = min(t + timedelta(hours=1), end)
        known = pv.until is not None and t < pv.until
        out.append([minutes(t, start),
                    round(pv.kwh(t, t2, corrected=False), 3) if known else None,
                    round(pv.kwh(t, t2), 3) if known else None,
                    round(load_kwh(t, t2), 3)])
        t = t2
    return out


def day_ahead(plan: Plan, pv: PvForecast, load_kwh: LoadFn, start: datetime, end: datetime) -> dict:
    """What the plan expected for the rest of the day, and the forecasts it was based on."""
    t0 = max(plan.created, start)
    return {"created": minutes(plan.created, start), "soc": plan_points(plan, t0, end, start),
            "actions": plan_actions(plan, t0, end, start), "hours": forecast_hours(pv, load_kwh, start, end)}


def hour_ahead(plan: Plan, pv: PvForecast, load_kwh: LoadFn, hour: datetime, start: datetime) -> dict:
    """What the plan in force at the start of this clock hour expected for the hour."""
    end = hour + timedelta(hours=1)
    t0 = max(plan.created, hour)
    known = pv.until is not None and hour < pv.until
    return {"hour": minutes(hour, start), "created": minutes(plan.created, start),
            "soc": plan_points(plan, t0, end, start), "actions": plan_actions(plan, t0, end, start),
            "pv_raw": round(pv.kwh(hour, end, corrected=False), 3) if known else None,
            "pv": round(pv.kwh(hour, end), 3) if known else None,
            "load": round(load_kwh(hour, end), 3)}


# ---------------------------------------------------------------- what happened
def measured(records: list[QuarterRecord], start: datetime, end: datetime) -> dict:
    """Per quarter from start to end: SOC at its start (plus one at the end), kWh per channel
    (scaled up when part of the quarter is missing, None below half) and the inverter mode."""
    by_start = {r.start: r for r in records}
    n = minutes(end, start) // 15
    out: dict[str, list] = {"soc": [], "pv": [], "load": [], "import": [], "export": [], "mode": []}
    for i in range(n + 1):
        r = by_start.get(start + QUARTER * i)
        out["soc"].append(None if r is None or r.soc is None else round(r.soc, 1))
        if i == n:
            break
        for ch in ("pv", "load", "import", "export"):
            cov = r.covered.get(ch, 0) if r else 0
            out[ch].append(round(r.kwh.get(ch, 0.0) / cov, 4) if cov >= 0.5 else None)
        out["mode"].append(r.mode if r else None)
    return out


# ---------------------------------------------------------------- accuracy
def interp(points: list[list], m: float) -> float | None:
    """SOC of a point list at minute m (None outside it)."""
    if not points or m < points[0][0] or m > points[-1][0]:
        return None
    for (m0, s0), (m1, s1) in zip(points, points[1:]):
        if m0 <= m <= m1:
            return s0 if m1 == m0 else s0 + (s1 - s0) * (m - m0) / (m1 - m0)
    return points[-1][1]


def _hour_sum(values: list, m0: int, m1: int) -> float | None:
    """Sum of the quarters in [m0, m1), None when one of them is missing."""
    qs = values[m0 // 15:m1 // 15]
    return None if not qs or any(v is None for v in qs) else sum(qs)


def _r(v, d=2):
    return None if v is None else round(v, d)


def _errors(diffs: list[float]) -> dict:
    if not diffs:
        return {"soc_mean_abs": None, "soc_max_abs": None}
    worst = max(diffs, key=abs)
    return {"soc_mean_abs": _r(statistics.fmean(abs(d) for d in diffs), 1), "soc_max_abs": _r(worst, 1)}


def summarize(doc: dict) -> dict:
    meas = doc["measured"]
    n = len(meas["mode"])
    soc = meas["soc"]
    seen = [m for m in meas["mode"] if m is not None]
    out = {"date": doc["date"], "minutes": doc["minutes"],
           "complete": sum(v is not None for v in meas["load"]) >= COMPLETE * n,
           "active_share": _r(sum(m != "shadow" for m in seen) / len(seen)) if seen else None,
           "solar_measured": _r(sum(v for v in meas["pv"] if v is not None)),
           "house_measured": _r(sum(v for v in meas["load"] if v is not None)),
           "day_ahead": None, "hourly": None}

    da = doc.get("day_ahead")
    if da:
        fc = raw = pv = house_fc = house = 0.0
        hours = 0
        for m0, f_raw, f_pv, f_load in da["hours"]:
            m1 = min(m0 + 60, doc["minutes"])
            got = _hour_sum(meas["pv"], m0, m1)
            if got is not None and f_pv is not None:
                raw, fc, pv, hours = raw + f_raw, fc + f_pv, pv + got, hours + 1
            ld = _hour_sum(meas["load"], m0, m1)
            if ld is not None:
                house_fc, house = house_fc + f_load, house + ld
        diffs = []
        for i, s in enumerate(soc):
            p = interp(da["soc"], i * 15) if s is not None and i * 15 >= da["created"] else None
            if p is not None:
                diffs.append(s - p)
        end_meas = next((s for s in reversed(soc) if s is not None), None)
        out["day_ahead"] = {"made": da["created"], "solar_hours": hours, "solar_forecast": _r(raw),
                            "solar_after_shade": _r(fc), "solar_measured": _r(pv),
                            "house_expected": _r(house_fc), "house_measured": _r(house),
                            "soc_end_planned": _r(da["soc"][-1][1], 1) if da["soc"] else None,
                            "soc_end_measured": end_meas, **_errors(diffs)}

    hourly = doc.get("hourly") or []
    if hourly:
        raw = fc = pv = house_fc = house = 0.0
        hours = 0
        diffs = []
        for h in hourly:
            m0, m1 = h["hour"], min(h["hour"] + 60, doc["minutes"])
            got = _hour_sum(meas["pv"], m0, m1)
            if got is not None and h["pv"] is not None:
                raw, fc, pv, hours = raw + h["pv_raw"], fc + h["pv"], pv + got, hours + 1
            ld = _hour_sum(meas["load"], m0, m1)
            if ld is not None:
                house_fc, house = house_fc + h["load"], house + ld
            if h["soc"] and h["soc"][-1][0] % 15 == 0 and h["soc"][-1][0] // 15 < len(soc):
                s = soc[h["soc"][-1][0] // 15]
                if s is not None:
                    diffs.append(s - h["soc"][-1][1])
        out["hourly"] = {"hours": len(hourly), "solar_hours": hours, "solar_forecast": _r(raw),
                         "solar_after_shade": _r(fc), "solar_measured": _r(pv),
                         "house_expected": _r(house_fc), "house_measured": _r(house), **_errors(diffs)}
    return out


def build_day(day: date, tz: ZoneInfo, records: list[QuarterRecord], log: dict | None,
              live: dict | None = None) -> dict:
    """One day: what was measured, what was predicted, and how far apart they were."""
    start, end = day_bounds(day, tz)
    log = log or {}
    doc = {"date": day.isoformat(), "start": start.isoformat(), "minutes": minutes(end, start),
           "measured": measured(records, start, end), "day_ahead": log.get("day_ahead"),
           "hourly": sorted((log.get("hours") or {}).values(), key=lambda h: h["hour"])}
    if live is not None:
        doc["live"] = live
    doc["summary"] = summarize(doc)
    return doc


# ---------------------------------------------------------------- storage
class HistoryStore:
    """One JSON file per closed day in `path`."""

    def __init__(self, path: str):
        self.path = path
        self._index: dict[str, dict] | None = None

    def _file(self, day: date) -> str:
        return os.path.join(self.path, day.isoformat() + ".json")

    def write(self, doc: dict) -> None:
        os.makedirs(self.path, exist_ok=True)
        f = self._file(date.fromisoformat(doc["date"]))
        with open(f + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(doc, fh, separators=(",", ":"))
        os.replace(f + ".tmp", f)
        self.index()
        self._index[doc["date"]] = doc["summary"]

    def read(self, day: date) -> dict | None:
        try:
            with open(self._file(day), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def days(self) -> list[str]:
        try:
            names = os.listdir(self.path)
        except OSError:
            return []
        return sorted(m.group(1) for m in map(FILE_RE.match, names) if m)

    def index(self) -> list[dict]:
        """Summaries of the stored days, oldest first."""
        if self._index is None:
            self._index = {}
            for d in self.days():
                doc = self.read(date.fromisoformat(d))
                if doc and isinstance(doc.get("summary"), dict):
                    self._index[d] = doc["summary"]
        return [self._index[d] for d in sorted(self._index)]

    def prune(self, today: date, keep: int = HISTORY_DAYS) -> None:
        cutoff = (today - timedelta(days=keep)).isoformat()
        for d in self.days():
            if d <= cutoff:
                try:
                    os.remove(self._file(date.fromisoformat(d)))
                except OSError:
                    pass
                if self._index is not None:
                    self._index.pop(d, None)
