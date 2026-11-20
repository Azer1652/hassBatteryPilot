"""Solar forecast, corrected for shade the forecast does not know about.

Forecast.Solar (or Solcast) models the panels, not the trees next to them.
Trees shade the panels mostly when the sun is low: in winter, and in the
morning or evening. `ShadeModel` learns the ratio actual / forecast for every
week of the year x hour of the day, so a winter afternoon behind a tree gets its
own factor and a summer noon keeps its own. A day's factor blends the weeks
around it, the nearest most: it follows the sun through the seasons day by day
and still averages out the weather. Hours are on winter time, so the summer-time
clock change does not move the sun to another cell. Hours without trustworthy
data are skipped (a missing PV sensor must not teach the model that it is always
dark, which is what broke the old single correction factor).
"""
from __future__ import annotations

import calendar
import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .meter import QuarterRecord

MIN_FORECAST_KWH = 0.05     # ignore hours with almost no forecast (dawn, dusk, night)
MAX_WEIGHT_KWH = 30.0       # memory: older data fades once this much forecast energy is seen in a week
PRIOR_KWH = 0.5             # a little pull towards 1.0 for hours with little data
CLAMP = (0.05, 1.5)
WEEKS = 52                  # the year in cells of about 7 days
BLEND_WEEKS = 4.0           # a day's factor blends the weeks within this distance, the nearest most
FADE_HALF_LIFE_DAYS = 365.0  # what was learned counts half a year later: a felled tree is learned in time

# Today's weather against the forecast (`intraday_factor`)
NOWCAST_WINDOW_H = 3.0      # the measured quarters that count
NOWCAST_PRIOR_KWH = 0.5     # pull towards 1.0: a dim dawn says little about the day
NOWCAST_HALF_LIFE_H = 4.0   # the correction fades: clouds now say less about this evening
NOWCAST_CLAMP = (0.3, 1.6)


def year_position(day: date) -> float:
    """Where a day lies in its year, in weeks from 1 January (0 to 52): week cell k holds [k, k + 1)."""
    days = 366 if calendar.isleap(day.year) else 365
    return (day.timetuple().tm_yday - 0.5) / days * WEEKS


def week_of(day: date) -> int:
    return min(int(year_position(day)), WEEKS - 1)


def week_days(k: int, year: int) -> tuple[date, date]:
    """The first and the middle day of week cell k in a year."""
    days = 366 if calendar.isleap(year) else 365
    jan1 = date(year, 1, 1)
    return (jan1 + timedelta(days=math.ceil(k * days / WEEKS - 0.5)),
            jan1 + timedelta(days=int((k + 0.5) * days / WEEKS)))


def dst_hours(day: date, tz: ZoneInfo) -> int:
    """How far the clock is ahead of winter time on a day (1 in summer time, else 0)."""
    return round((datetime.combine(day, time(12), tz).dst() or timedelta(0)).total_seconds() / 3600)


def winter_time(hour: datetime, tz: ZoneInfo) -> tuple[date, int]:
    """(date, hour) of a moment on the zone's winter (standard) time. The sun stands in the same
    place at the same winter-time hour all year; the clock moves an hour in summer."""
    local = hour.astimezone(tz)
    std = local.replace(tzinfo=None) - (local.dst() or timedelta(0))
    return std.date(), std.hour


class ShadeModel:
    """actual / forecast per week of the year (`WEEKS` rows) and winter-time hour (24 columns), each
    a running mean weighted by the forecast kWh it was learned from (`weights`)."""

    def __init__(self, factors=None, weights=None, faded: date | None = None):
        self.factors = factors or [[1.0] * 24 for _ in range(WEEKS)]
        self.weights = weights or [[0.0] * 24 for _ in range(WEEKS)]
        self.faded = faded              # the day `fade_to` last ran for
        self.migrated = False           # loaded from the month x clock-hour model (2.21.0 and before)
        self._memo: dict[tuple[date, int], float] = {}

    def learn(self, day: date, hour: int, actual_kwh: float, forecast_kwh: float) -> bool:
        """Learn one hour: `day` and `hour` on winter time (`winter_time`)."""
        if forecast_kwh < MIN_FORECAST_KWH or actual_kwh < 0:
            return False
        r = min(max(actual_kwh / forecast_kwh, CLAMP[0]), CLAMP[1])
        k = week_of(day)
        w_old = max(min(self.weights[k][hour], MAX_WEIGHT_KWH - forecast_kwh), 0.0)
        self.factors[k][hour] = (self.factors[k][hour] * w_old + r * forecast_kwh) / (w_old + forecast_kwh)
        self.weights[k][hour] = w_old + forecast_kwh
        self._memo.clear()
        return True

    def factor(self, day: date, hour: int) -> float:
        """Factor for a day and winter-time hour: the week cells within `BLEND_WEEKS` of the day,
        each by what it learned and how near it is, so it changes a little every day."""
        key = (day, hour)
        f = self._memo.get(key)            # (a cache: the nightly replay thread fills it too; only learning clears it)
        if f is not None:
            return f
        p = year_position(day)
        acc = tot = PRIOR_KWH
        for k in range(math.floor(p - 0.5 - BLEND_WEEKS), math.ceil(p - 0.5 + BLEND_WEEKS) + 1):
            near = 1 - abs(p - (k + 0.5)) / BLEND_WEEKS
            if near <= 0:
                continue
            w = self.weights[k % WEEKS][hour] * near
            acc += self.factors[k % WEEKS][hour] * w
            tot += w
        f = self._memo[key] = acc / tot
        return f

    def at(self, hour: datetime, tz: ZoneInfo) -> float:
        """Factor for the hour that starts at `hour` (any time zone)."""
        return self.factor(*winter_time(hour, tz))

    def fade_to(self, day: date) -> None:
        """Older learning counts less: the weights halve every `FADE_HALF_LIFE_DAYS`. It remembers the day
        it faded to, so a restart or a skipped night neither fades twice nor misses a day."""
        if self.faded is not None and day > self.faded:
            k = 0.5 ** ((day - self.faded).days / FADE_HALF_LIFE_DAYS)
            self.weights = [[w * k for w in row] for row in self.weights]
            self._memo.clear()
        if self.faded is None or day > self.faded:
            self.faded = day

    def month_summary(self, year: int = 2026) -> list[float | None]:
        """Forecast-weighted factor per month (None while nothing is learned), for display."""
        acc, tot = [0.0] * 12, [0.0] * 12
        for k in range(WEEKS):
            m = week_days(k, year)[1].month - 1
            acc[m] += sum(f * w for f, w in zip(self.factors[k], self.weights[k]))
            tot[m] += sum(self.weights[k])
        return [round(a / t, 3) if t > 0 else None for a, t in zip(acc, tot)]

    def display(self, year: int, tz: ZoneInfo) -> dict:
        """For the page, on the clock: per week cell its first day ("MM-DD"), the factor the plan uses
        around its middle day per clock hour, and the forecast kWh the week itself learned from."""
        weeks, factors, kwh = [], [], []
        for k in range(WEEKS):
            first, middle = week_days(k, year)
            shift = dst_hours(middle, tz)
            weeks.append(first.strftime("%m-%d"))
            factors.append([round(self.factor(middle, h - shift), 3) if h >= shift else 1.0 for h in range(24)])
            kwh.append([round(self.weights[k][h - shift], 1) if h >= shift else 0.0 for h in range(24)])
        return {"weeks": weeks, "factors": factors, "kwh": kwh}

    def learn_records(self, records: list[QuarterRecord], forecast_archive: dict[str, float], tz: ZoneInfo,
                      export_limit_kw: float | None = None, ac_max_kw: float | None = None) -> int:
        """Learn from closed quarters and the hourly forecast that was archived for them.
        Hours in which the inverter may have held the panels back (`held_back`: export blocked,
        at the export limit or at its AC output) are skipped: that is not shade."""
        by_hour: dict[datetime, list[QuarterRecord]] = {}
        for r in records:
            by_hour.setdefault(r.start.replace(minute=0), []).append(r)
        n = 0
        for hour, recs in by_hour.items():
            fc = forecast_archive.get(hour.isoformat())
            if fc is None or len(recs) < 4 or any(r.covered.get("pv", 0) < 0.9
                                                  or held_back(r, export_limit_kw, ac_max_kw) for r in recs):
                continue
            n += self.learn(*winter_time(hour, tz), sum(r.kwh["pv"] for r in recs), fc)
        return n

    def learn_hourly(self, actual: dict[datetime, tuple[float, float]], forecast: dict[datetime, tuple[float, float]],
                     tz: ZoneInfo) -> int:
        """Bootstrap from recorder history: {UTC hour: (kWh, coverage)} for PV and forecast power."""
        n = 0
        for hour, (pv, cov) in actual.items():
            fc = forecast.get(hour)
            if fc is None or cov < 0.9 or fc[1] < 0.9:
                continue
            n += self.learn(*winter_time(hour, tz), pv, fc[0])
        return n

    def to_json(self):
        return {"per": "week", "faded": self.faded.isoformat() if self.faded else None,
                "factors": [[round(f, 4) for f in row] for row in self.factors],
                "weights": [[round(w, 3) for w in row] for row in self.weights]}

    @classmethod
    def from_json(cls, d, tz: ZoneInfo | None = None):
        if not d:
            return cls()
        factors = [list(map(float, r)) for r in d["factors"]]
        weights = [list(map(float, r)) for r in d["weights"]]
        rows = 12 if len(factors) == 12 else WEEKS
        if any(len(g) != rows or any(len(r) != 24 for r in g) for g in (factors, weights)):
            raise ValueError("shade: not a weeks x 24 (or months x 24) grid")
        if rows == 12:
            return cls.from_months(factors, weights, tz or ZoneInfo("UTC"))
        return cls(factors, weights, date.fromisoformat(d["faded"]) if d.get("faded") else None)

    @classmethod
    def from_months(cls, factors: list[list[float]], weights: list[list[float]], tz: ZoneInfo) -> ShadeModel:
        """The model of 2.21.0 and before (month x clock hour) carried over: each week takes its middle
        day's month at the same sun (clock hour = winter hour + that day's summer time), with the
        month's weight shared over its weeks, so it counts as much as before."""
        m = cls()
        for k in range(WEEKS):
            middle = week_days(k, 2026)[1]
            shift = dst_hours(middle, tz)
            for h in range(24):
                clock = min(h + shift, 23)
                m.factors[k][h] = factors[middle.month - 1][clock]
                m.weights[k][h] = weights[middle.month - 1][clock] * 7 / 30.4
        m.migrated = True
        return m


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


def held_back(r: QuarterRecord, export_limit_kw: float | None = None, ac_max_kw: float | None = None) -> bool:
    """The inverter may have held the panels back in this quarter: export blocked, export at the
    export limit, or its AC output (house + export - import) at its rating (`inverter_ac_kw`)."""
    if r.blocked:
        return True
    k = r.kwh
    if export_limit_kw and k.get("export", 0.0) * 4 >= 0.95 * export_limit_kw:
        return True
    ac = (k.get("load", 0.0) + k.get("export", 0.0) - k.get("import", 0.0)) * 4
    return bool(ac_max_kw) and ac >= 0.95 * ac_max_kw


def intraday_factor(records: list[QuarterRecord], hourly: dict[datetime, float], shade: ShadeModel,
                    now: datetime, tz: ZoneInfo, export_limit_kw: float | None = None,
                    ac_max_kw: float | None = None) -> tuple[float, float]:
    """(measured / expected solar over the last hours of today, the expected kWh it rests on).

    The shade factors are an average over weeks; this is today: a day much cloudier or clearer
    than forecast. Quarters in which the inverter held the panels back (`held_back`) are
    skipped: that is not weather."""
    today = now.astimezone(tz).date()
    quarter = timedelta(minutes=15)
    actual = expected = 0.0
    for r in records:
        if r.start < now - timedelta(hours=NOWCAST_WINDOW_H) or r.start + quarter > now:
            continue
        if r.start.astimezone(tz).date() != today or r.covered.get("pv", 0) < 0.9:
            continue
        if held_back(r, export_limit_kw, ac_max_kw):
            continue
        hour = r.start.replace(minute=0, second=0, microsecond=0)
        fc = hourly.get(hour)
        if fc is None:
            continue
        expected += fc / 4 * shade.at(hour, tz)
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
                export_limit_kw: float | None = None, ac_max_kw: float | None = None) -> float:
        """Compare today's measured solar with the forecast and correct the rest of today.
        `past`: the forecast as it was for hours that have started, where `hourly` no longer has them."""
        factor, _ = intraday_factor(records, {**self.hourly, **(past or {})}, self.shade, now, self.tz,
                                    export_limit_kw, ac_max_kw)
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
        return self.hourly.get(hour, 0.0) * self.shade.at(hour, self.tz) * self.today_factor(hour)

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
