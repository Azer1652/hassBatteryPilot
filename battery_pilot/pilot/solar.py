"""Solar forecast, corrected for shade the forecast does not know about.

Forecast.Solar (or Solcast) models the panels, not the trees next to them.
Trees shade the panels mostly when the sun is low: in winter, and in the
morning or evening. `ShadeModel` learns the ratio actual / forecast for every
month x hour of the day, so a winter afternoon behind a tree gets its own
factor and a summer noon keeps its own. Hours without trustworthy data are
skipped (a missing PV sensor must not teach the model that it is always dark,
which is what broke the old single correction factor).
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .meter import QuarterRecord

MIN_FORECAST_KWH = 0.05     # ignore hours with almost no forecast (dawn, dusk, night)
MAX_WEIGHT_KWH = 30.0       # memory: older data fades once this much forecast energy is seen
PRIOR_KWH = 0.5             # a little pull towards 1.0 for cells with little data
CLAMP = (0.05, 1.5)

# Today's weather against the forecast (`intraday_factor`)
NOWCAST_WINDOW_H = 3.0      # the measured quarters that count
NOWCAST_PRIOR_KWH = 0.5     # pull towards 1.0: a dim dawn says little about the day
NOWCAST_HALF_LIFE_H = 4.0   # the correction fades: clouds now say less about this evening
NOWCAST_CLAMP = (0.3, 1.6)


class ShadeModel:
    def __init__(self, factors=None, weights=None):
        self.factors = factors or [[1.0] * 24 for _ in range(12)]
        self.weights = weights or [[0.0] * 24 for _ in range(12)]

    def learn(self, month: int, hour: int, actual_kwh: float, forecast_kwh: float) -> bool:
        if forecast_kwh < MIN_FORECAST_KWH or actual_kwh < 0:
            return False
        r = min(max(actual_kwh / forecast_kwh, CLAMP[0]), CLAMP[1])
        m = month - 1
        w_old = max(min(self.weights[m][hour], MAX_WEIGHT_KWH - forecast_kwh), 0.0)
        self.factors[m][hour] = (self.factors[m][hour] * w_old + r * forecast_kwh) / (w_old + forecast_kwh)
        self.weights[m][hour] = w_old + forecast_kwh
        return True

    def factor(self, month: int, hour: int) -> float:
        """Factor for a month and local hour; borrows from the next months while it has little data."""
        m = month - 1
        acc, tot = PRIOR_KWH, PRIOR_KWH
        w = self.weights[m][hour]
        acc, tot = acc + self.factors[m][hour] * w, tot + w
        if w < 2.0:
            for dm in (-1, 1):
                wn = self.weights[(m + dm) % 12][hour] * 0.5
                acc, tot = acc + self.factors[(m + dm) % 12][hour] * wn, tot + wn
        return acc / tot

    def month_summary(self) -> list[float | None]:
        """Forecast-weighted factor per month (None while nothing is learned), for display."""
        out = []
        for m in range(12):
            w = sum(self.weights[m])
            out.append(round(sum(f * x for f, x in zip(self.factors[m], self.weights[m])) / w, 3) if w > 0 else None)
        return out

    def learn_records(self, records: list[QuarterRecord], forecast_archive: dict[str, float], tz: ZoneInfo) -> int:
        """Learn from closed quarters and the hourly forecast that was archived for them.
        Hours in which export was blocked are skipped: the inverter held the panels back then,
        and that is not shade."""
        by_hour: dict[datetime, list[QuarterRecord]] = {}
        for r in records:
            by_hour.setdefault(r.start.replace(minute=0), []).append(r)
        n = 0
        for hour, recs in by_hour.items():
            fc = forecast_archive.get(hour.isoformat())
            if fc is None or len(recs) < 4 or any(r.covered.get("pv", 0) < 0.9 or r.blocked for r in recs):
                continue
            local = hour.astimezone(tz)
            n += self.learn(local.month, local.hour, sum(r.kwh["pv"] for r in recs), fc)
        return n

    def learn_hourly(self, actual: dict[datetime, tuple[float, float]], forecast: dict[datetime, tuple[float, float]],
                     tz: ZoneInfo) -> int:
        """Bootstrap from recorder history: {UTC hour: (kWh, coverage)} for PV and forecast power."""
        n = 0
        for hour, (pv, cov) in actual.items():
            fc = forecast.get(hour)
            if fc is None or cov < 0.9 or fc[1] < 0.9:
                continue
            local = hour.astimezone(tz)
            n += self.learn(local.month, local.hour, pv, fc[0])
        return n

    def to_json(self):
        return {"factors": [[round(f, 4) for f in row] for row in self.factors],
                "weights": [[round(w, 3) for w in row] for row in self.weights]}

    @classmethod
    def from_json(cls, d):
        if not d:
            return cls()
        return cls([list(map(float, r)) for r in d["factors"]], [list(map(float, r)) for r in d["weights"]])


def parse_energy_forecast(result: dict | None) -> dict[datetime, float]:
    """`energy/solar_forecast` websocket result -> {UTC hour start: kWh}, summed over all planes."""
    out: dict[datetime, float] = {}
    for entry in (result or {}).values():
        for iso, wh in ((entry or {}).get("wh_hours") or {}).items():
            try:
                t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            except ValueError:
                continue
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            t = t.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
            out[t] = out.get(t, 0.0) + float(wh) / 1000
    return out


def hourly_from_daily(day: date, total_kwh: float, latitude: float, longitude: float, tz: ZoneInfo) -> dict[datetime, float]:
    """Spread a daily forecast total over the daylight hours (sine shape), when no hourly forecast exists."""
    n = day.timetuple().tm_yday
    decl = math.radians(23.44) * math.sin(2 * math.pi * (284 + n) / 365)
    x = -math.tan(math.radians(latitude)) * math.tan(decl)
    half = math.degrees(math.acos(min(max(x, -1.0), 1.0))) / 15     # hours from solar noon to sunset
    noon = 12 - longitude / 15                                        # solar noon in UTC hours
    start_utc = datetime.combine(day, time(0), tz).astimezone(timezone.utc).replace(minute=0)
    weights = {}
    for k in range(-2, 27):
        h0 = start_utc + timedelta(hours=k)
        mid = h0.hour + 0.5 + (h0.date() - day).days * 24
        u = (mid - (noon - half)) / (2 * half) if half > 0 else -1
        if 0 < u < 1 and h0.astimezone(tz).date() == day:
            weights[h0] = math.sin(math.pi * u)
    tot = sum(weights.values())
    return {h: total_kwh * w / tot for h, w in weights.items()} if tot > 0 else {}


def intraday_factor(records: list[QuarterRecord], hourly: dict[datetime, float], shade: ShadeModel,
                    now: datetime, tz: ZoneInfo, export_limit_kw: float | None = None) -> tuple[float, float]:
    """(measured / expected solar over the last hours of today, the expected kWh it rests on).

    The shade factors are an average over weeks; this is today: a day much cloudier or clearer
    than forecast. Quarters in which the inverter held the panels back (export blocked, or at
    the export limit) are skipped: that is not weather."""
    today = now.astimezone(tz).date()
    quarter = timedelta(minutes=15)
    actual = expected = 0.0
    for r in records:
        if r.start < now - timedelta(hours=NOWCAST_WINDOW_H) or r.start + quarter > now:
            continue
        if r.start.astimezone(tz).date() != today or r.covered.get("pv", 0) < 0.9 or r.blocked:
            continue
        if export_limit_kw and r.kwh.get("export", 0.0) * 4 >= 0.95 * export_limit_kw:
            continue
        hour = r.start.replace(minute=0, second=0, microsecond=0)
        fc = hourly.get(hour)
        if fc is None:
            continue
        local = hour.astimezone(tz)
        expected += fc / 4 * shade.factor(local.month, local.hour)
        actual += max(r.kwh.get("pv", 0.0), 0.0)
    f = (actual + NOWCAST_PRIOR_KWH) / (expected + NOWCAST_PRIOR_KWH)
    return min(max(f, NOWCAST_CLAMP[0]), NOWCAST_CLAMP[1]), expected


class PvForecast:
    """Hourly solar forecast (UTC hour -> kWh) with the shade correction applied, and for the
    rest of today the correction for today's weather (`observe`)."""

    def __init__(self, hourly: dict[datetime, float], shade: ShadeModel, tz: ZoneInfo):
        self.hourly = hourly
        self.shade = shade
        self.tz = tz
        self.nowcast: tuple[float, datetime] | None = None     # (factor, measured at)

    def observe(self, records: list[QuarterRecord], now: datetime, past: dict[datetime, float] | None = None,
                export_limit_kw: float | None = None) -> float:
        """Compare today's measured solar with the forecast and correct the rest of today.
        `past`: the forecast as it was for hours that have started, where `hourly` no longer has them."""
        factor, _ = intraday_factor(records, {**self.hourly, **(past or {})}, self.shade, now, self.tz, export_limit_kw)
        self.nowcast = (factor, now) if abs(factor - 1) > 1e-6 else None
        return factor

    def today_factor(self, hour: datetime) -> float:
        """Today's weather correction for an hour: full for the hour that is running, fading after it,
        none for tomorrow or for hours that are over."""
        if self.nowcast is None:
            return 1.0
        factor, at = self.nowcast
        if hour + timedelta(hours=1) <= at or hour.astimezone(self.tz).date() != at.astimezone(self.tz).date():
            return 1.0
        ahead = max((hour - at).total_seconds() / 3600, 0.0)
        return 1 + (factor - 1) * 0.5 ** (ahead / NOWCAST_HALF_LIFE_H)

    @property
    def until(self) -> datetime | None:
        return max(self.hourly) + timedelta(hours=1) if self.hourly else None

    def corrected(self, hour: datetime) -> float:
        raw = self.hourly.get(hour, 0.0)
        local = hour.astimezone(self.tz)
        return raw * self.shade.factor(local.month, local.hour) * self.today_factor(hour)

    def kwh(self, t0: datetime, t1: datetime, corrected: bool = True) -> float:
        total = 0.0
        hour = t0.replace(minute=0, second=0, microsecond=0)
        while hour < t1:
            nxt = hour + timedelta(hours=1)
            overlap = (min(t1, nxt) - max(t0, hour)).total_seconds() / 3600
            if overlap > 0:
                total += (self.corrected(hour) if corrected else self.hourly.get(hour, 0.0)) * overlap
            hour = nxt
        return total

    def day_total(self, day: date, corrected: bool = True) -> float:
        start = datetime.combine(day, time(0), self.tz).astimezone(timezone.utc)
        end = datetime.combine(day + timedelta(days=1), time(0), self.tz).astimezone(timezone.utc)
        if corrected:
            return self.kwh(start, end)
        return sum(v for h, v in self.hourly.items() if start <= h < end)


def archive_forecast(archive: dict[str, float], hourly: dict[datetime, float], now: datetime,
                     keep: timedelta = timedelta(days=3)) -> dict[str, float]:
    """Remember the latest forecast for each hour that has not started yet (for learning later)."""
    out = {k: v for k, v in archive.items() if datetime.fromisoformat(k) > now - keep}
    for h, v in hourly.items():
        if h > now:
            out[h.isoformat()] = round(v, 4)
    return out
