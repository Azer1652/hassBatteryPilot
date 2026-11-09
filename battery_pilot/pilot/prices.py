"""Market prices from whatever price integration the user has.

Every source is turned into a list of `PriceSlot` in EUR/MWh (the unit the
contract formulas use), sorted by start time, 15 or 60 minutes long.

Supported attribute layouts:
* ENTSO-e (HACS, JaccoR/hass-entso-e): `prices` or `prices_today` + `prices_tomorrow`,
  items `{"time": ..., "price": ...}`, usually EUR/kWh.
* Nord Pool (HACS, custom-components/nordpool): `raw_today` + `raw_tomorrow`,
  items `{"start": ..., "end": ..., "value": ...}`.
* Generic lists in `forecast` / `data` with a start key and a price key.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

TIME_KEYS = ("start", "time", "start_time", "startsAt", "from", "datetime")
END_KEYS = ("end", "end_time", "endsAt", "till", "to")
PRICE_KEYS = ("price", "value", "price_per_kwh", "total", "electricity_price")
LIST_ATTRS = (("prices",), ("raw_today", "raw_tomorrow"), ("prices_today", "prices_tomorrow"),
              ("forecast",), ("data",))


@dataclass(frozen=True)
class PriceSlot:
    start: datetime   # aware, UTC
    end: datetime
    eur_mwh: float

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


def parse_time(value) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def unit_factor(unit: str | None, sample: list[float]) -> float:
    """Multiplier that turns the source unit into EUR/MWh."""
    u = (unit or "").replace(" ", "").lower()
    if "mwh" in u:
        return 1.0
    if "kwh" in u:
        if u.startswith(("c/", "ct", "cent", "c€", "¢")):
            return 10.0
        return 1000.0
    # No unit: guess from the size of the numbers (EUR/kWh is rarely above 2).
    if sample and statistics.median(abs(x) for x in sample) < 2:
        return 1000.0
    return 1.0


def _items(attrs: dict) -> list:
    for group in LIST_ATTRS:
        lists = [attrs.get(a) for a in group]
        if any(isinstance(x, list) and x for x in lists):
            out = []
            for x in lists:
                if isinstance(x, list):
                    out.extend(x)
            return out
    return []


def parse_state(state: dict | None) -> list[PriceSlot]:
    """Price slots from a price sensor's state object (as returned by /api/states)."""
    if not state:
        return []
    attrs = state.get("attributes") or {}
    raw = []
    for item in _items(attrs):
        if not isinstance(item, dict):
            continue
        start = next((parse_time(item[k]) for k in TIME_KEYS if k in item), None)
        price = next((item[k] for k in PRICE_KEYS if k in item), None)
        end = next((parse_time(item[k]) for k in END_KEYS if k in item), None)
        if start is None or price is None:
            continue
        try:
            raw.append((start, end, float(price)))
        except (TypeError, ValueError):
            continue
    if not raw:
        return []
    raw.sort(key=lambda r: r[0])
    factor = unit_factor(attrs.get("unit_of_measurement"), [r[2] for r in raw])
    steps = [(b[0] - a[0]) for a, b in zip(raw, raw[1:]) if b[0] > a[0]]
    default_len = min(steps) if steps else timedelta(hours=1)
    slots: dict[datetime, PriceSlot] = {}
    for start, end, price in raw:
        if end is None or end <= start:
            end = start + default_len
        slots[start] = PriceSlot(start, end, round(price * factor, 4))
    return sorted(slots.values(), key=lambda s: s.start)


def price_at(slots: list[PriceSlot], t: datetime) -> float | None:
    for s in slots:
        if s.start <= t < s.end:
            return s.eur_mwh
    return None


def merge(old: list[PriceSlot], new: list[PriceSlot], keep_after: datetime) -> list[PriceSlot]:
    """Archive: new slots win, slots that ended before `keep_after` are dropped."""
    by_start = {s.start: s for s in old if s.end > keep_after}
    by_start.update({s.start: s for s in new})
    return sorted(by_start.values(), key=lambda s: s.start)


def to_json(slots: list[PriceSlot]) -> list:
    return [[s.start.isoformat(), s.minutes, s.eur_mwh] for s in slots]


def from_json(data: list) -> list[PriceSlot]:
    out = []
    for start, minutes, price in data or []:
        st = parse_time(start)
        if st is not None:
            out.append(PriceSlot(st, st + timedelta(minutes=int(minutes)), float(price)))
    return out
