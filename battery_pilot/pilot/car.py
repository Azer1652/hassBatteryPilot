"""Is the electric car charging, and how much power does it take?

Battery Pilot does not control the charger. It only has to know when the car charges: a car on
the house circuit looks like house consumption, so the home battery would feed it at full power,
run empty and leave the whole charge to the grid (a capacity peak), and the session would be
learned as "usual consumption".

The rest of the app sees a `CarState` and nothing else. Where it comes from is a *source*, an
object with `update(now, load_kw, charger_kw) -> CarState`:

* `LoadStepSource`    - no integration: a jump in house consumption that stays is the car;
* `ChargerPowerSource` - a power sensor of the charger, when Home Assistant has one.

A source that knows more about the car (its battery level, whether it is plugged in: a car
integration) only has to fill the other `CarState` fields and be chosen in `make_source`;
controller, plan, replay and page take the state as it is.
"""
from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import Options
from .simulate import Battery, step

START_AFTER = timedelta(minutes=3)      # the jump must last this long (a kettle does not)
END_AFTER = timedelta(minutes=2)        # and be gone this long
LOST_AFTER = timedelta(minutes=3)       # no reading for this long ends a session
BASELINE_WINDOW = timedelta(minutes=10)
CHARGER_ON_KW = 1.0                     # a charger sensor above this is a charging car


@dataclass(frozen=True)
class CarState:
    charging: bool = False
    power_kw: float = 0.0               # what the car takes now (0 when not charging)
    # Known only to a source that talks to the car (none does yet):
    soc: float | None = None            # the car's battery level, %
    plugged: bool | None = None
    target_soc: float | None = None


IDLE = CarState()


class NoCar:
    """Car detection is off."""

    def update(self, now: datetime, load_kw: float | None, charger_kw: float | None = None) -> CarState:
        return IDLE


class ChargerPowerSource:
    """The charger's own power reading."""

    def __init__(self):
        self.state, self.seen = IDLE, None

    def update(self, now: datetime, load_kw: float | None, charger_kw: float | None = None) -> CarState:
        if charger_kw is None:           # a short gap keeps the last state
            if self.seen is None or now - self.seen >= LOST_AFTER:
                self.state = IDLE
            return self.state
        self.seen = now
        self.state = CarState(True, round(charger_kw, 3)) if charger_kw >= CHARGER_ON_KW else IDLE
        return self.state


class LoadStepSource:
    """The car seen in the house consumption: at least `min_kw` more than the house used just
    before, for three minutes. Its power is what the house uses above that level. A long burst
    of the hob or oven can pass for a car; the worst that happens is peak shaving while cooking."""

    def __init__(self, min_kw: float):
        self.min_kw = min_kw
        self.samples: deque[tuple[datetime, float]] = deque()      # house load outside a session
        self.baseline: float | None = None                          # house load when the session began
        self.state = IDLE
        self.above_since: datetime | None = None
        self.below_since: datetime | None = None
        self.seen: datetime | None = None

    def _level(self) -> float | None:
        return statistics.median(v for _, v in self.samples) if self.samples else None

    def update(self, now: datetime, load_kw: float | None, charger_kw: float | None = None) -> CarState:
        if load_kw is None:
            if self.seen is None or now - self.seen >= LOST_AFTER:
                self._end()
            return self.state
        self.seen = now
        if self.state.charging:
            if load_kw < self.baseline + self.min_kw / 2:
                self.below_since = self.below_since or now
                if now - self.below_since >= END_AFTER:
                    self._end()
                    self.samples.append((now, load_kw))
                    return self.state
            else:
                self.below_since = None
            self.state = CarState(True, round(max(load_kw - self.baseline, 0.0), 3))
            return self.state
        level = self._level()
        if level is not None and load_kw >= level + self.min_kw:
            self.above_since = self.above_since or now      # (not added to the samples: it may be the car)
            if now - self.above_since >= START_AFTER:
                self.baseline, self.below_since = level, None
                self.state = CarState(True, round(load_kw - level, 3))
            return self.state
        self.above_since = None
        self.samples.append((now, load_kw))
        while self.samples and now - self.samples[0][0] > BASELINE_WINDOW:
            self.samples.popleft()
        return self.state

    def _end(self):
        self.state, self.baseline, self.above_since, self.below_since = IDLE, None, None, None
        self.samples.clear()             # the house may have changed during a long session


def make_source(o: Options, entities: dict):
    """The source for these settings: the charger's power sensor when one is set, else the jump
    in house consumption when `car_min_kw` is set, else none. (A car integration would be chosen here.)"""
    if "car_charger_power" in entities:
        return ChargerPowerSource()
    if o.car_min_kw > 0:
        return LoadStepSource(o.car_min_kw)
    return NoCar()


# ---------------------------------------------------------------- how much of the car fits under the limit
CAR_EFF = 0.9                   # charger and car losses: kWh into the car per kWh the charger takes
COVER_MAX = timedelta(hours=24)


@dataclass(frozen=True)
class Coverage:
    soc: float          # the home battery's SOC the car starts at, %
    pct: float          # of the car's battery charged before the import limit is crossed
    kwh: float          # into the car by then
    hours: float
    whole: bool         # the car is full before the home battery reaches its floor


def coverage(o: Options, soc: float, car_kw: float, floor: float, start: datetime,
             load_kwh, pv_kwh) -> Coverage:
    """How much of the car charges at `car_kw` while the grid gives up to the import limit and the
    home battery the rest (peak shaving), until the battery reaches `floor` and the import limit
    would be crossed. `load_kwh(a, b)` / `pv_kwh(a, b)`: the house's usual consumption and the solar
    forecast. The same physics as the replay (`simulate.step`, peak_shave with `shave_floor`)."""
    b = Battery.from_options(o)
    need = o.car_battery_kwh / CAR_EFF          # what the charger takes for an empty-to-full car
    limit = o.import_limit_kw
    got, t, s, whole = 0.0, start, soc, False
    while t - start < COVER_MAX:
        h = min(0.25, (need - got) / car_kw)
        t1 = t + timedelta(hours=h)
        house, pv = load_kwh(t, t1), pv_kwh(t, t1)
        res = step(b, s, "peak_shave", s, house + car_kw * h, pv, h, shave_floor=floor)
        over = res.import_kwh - limit * h
        if over > 1e-6:
            if res.soc <= max(floor, b.min_soc) + 1e-6:     # the battery reached its floor in this quarter
                above = car_kw * h + house - pv - limit * h      # what the battery had to give
                got += car_kw * h * max(1 - over / above, 0.0) if above > 0 else 0.0
            break                               # (else the battery cannot give that much power)
        got, s, t = got + car_kw * h, res.soc, t1
        if got >= need - 1e-9:
            whole = True
            break
    else:                                       # a whole day under the limit: the car can go on as slowly
        got, whole = need, True
    car = min(got * CAR_EFF, o.car_battery_kwh)
    return Coverage(round(soc), round(car / o.car_battery_kwh * 100), round(car, 1),
                    round(got / car_kw, 2), whole)
