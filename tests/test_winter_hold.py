"""The winter level: on a dull day self-use stops at `winter_hold_soc`, below it peak shaving, without
a grid refill, without flipping the inverter back and forth, and the car may still use it."""
from datetime import date, timedelta

import pytest
from fake_ha import TZ, FakeHA, utc
from test_car import day_of_quarters

from pilot.app import App
from pilot.compare import replay_day
from pilot.config import ConfigError, Options
from pilot.controller import WINTER_BAND, WINTER_DWELL_S, Readings, decide
from pilot.load import LoadProfile
from pilot.planner import Slot, Step, is_winter_day, make_plan, optimize, winter_hold
from pilot.simulate import Battery
from pilot.simulate import step as sim
from pilot.tariffs import tariff

CONTROL = "input_boolean.battery_pilot_control"
T0 = utc(2026, 1, 14, 17, 0)
OPTS = Options(battery_min_soc=10, winter_hold_soc=50)


def flat(kw):
    return lambda a, b: kw * (b - a).total_seconds() / 3600


# ---------------------------------------------------------------- setting and simulator
def test_the_setting_is_off_by_default_and_only_counts_above_the_peak_buffer():
    assert Options().winter_hold_soc == 0 and winter_hold(Options()) == 0
    assert winter_hold(OPTS) == 50
    assert winter_hold(Options(battery_min_soc=10, peak_buffer_pct=50, winter_hold_soc=50)) == 0
    with pytest.raises(ConfigError):
        Options(battery_min_soc=20, winter_hold_soc=15).validate()


def test_the_simulator_stops_self_use_at_the_level_and_does_not_refill():
    b = Battery.from_options(OPTS)
    r = sim(b, 55, "self_use", 0, 1.0, 0.0, 1.0, hold=50)            # 1 kW for an hour: only the 5 points above 50
    assert r.soc == pytest.approx(50) and r.import_kwh > 0.1 and r.charge_dc_kwh == 0
    r = sim(b, 45, "self_use", 0, 0.5, 0.0, 1.0, hold=50)             # below it, under the limit: all from the grid
    assert r.soc == 45 and r.import_kwh == pytest.approx(0.5)
    r = sim(b, 50, "self_use", 0, 1.25, 0.0, 0.25, hold=50)           # a 5 kW quarter: the grid at the limit, the rest
    assert r.import_kwh == pytest.approx(OPTS.import_limit_kw * 0.25) and r.soc < 50
    assert sim(b, 55, "self_use", 0, 1.0, 0.0, 1.0).soc < 50          # (no level: down as before)


# ---------------------------------------------------------------- planner
def plan(o, pv_kw, soc=60, winter_days=None, until=None):
    return make_plan(T0, TZ, o, tariff("fixed", o), [], flat(0.6), flat(pv_kw), soc, forecast_until=until,
                     peak_shave=True, winter_days=winter_days)


def test_a_dull_day_plan_stops_self_use_at_the_level():
    p = plan(OPTS, 0.0)
    assert all(s.hold == 50 for s in p.steps)
    assert min(s.soc_end for s in p.steps) == 50
    assert any(s.action == "peak_shave" and s.soc_start <= 50 for s in p.steps)
    sunny = plan(OPTS, 2.0)                                              # 2 kW all day: 48 kWh, not a winter day
    assert all(s.hold == 0 for s in sunny.steps)
    off = plan(Options(battery_min_soc=10), 0.0)
    assert all(s.hold == 0 for s in off.steps) and min(s.soc_end for s in off.steps) < 50
    no_switch = make_plan(T0, TZ, OPTS, tariff("fixed", OPTS), [], flat(0.6), flat(0.0), 60)
    assert all(s.hold == 0 for s in no_switch.steps)


def test_the_optimizer_takes_the_level_per_slot():
    b = Battery.from_options(OPTS)
    slots = [Slot(T0 + timedelta(hours=i), 1.0, 0.6, 0.0, 30.0, 5.0, None, 0.0, 50.0 if i < 5 else 0.0)
             for i in range(10)]
    steps = optimize(b, slots, 56, peak_shave=True)
    assert min(s.soc_end for s in steps[:5]) == 50                    # held on the winter day
    assert steps[-1].soc_end < 50                                     # the next day: self-use goes on


def test_a_winter_day_is_decided_once():
    days: dict[date, bool] = {}
    plan(OPTS, 0.0, winter_days=days)
    today = T0.astimezone(TZ).date()
    assert days[today] is True
    p = plan(OPTS, 2.0, winter_days=days)                                # the forecast changed: today stays winter
    assert all(s.hold == 50 for s in p.steps if s.start.astimezone(TZ).date() == today)
    late = is_winter_day(today + timedelta(days=2), TZ, OPTS, flat(0.0), T0 + timedelta(hours=20))
    assert late is None                                               # not forecast yet: not decided


def dull_then(tomorrow_kw):
    """A pv forecast: nothing today, `tomorrow_kw` from 08:00 to 16:00 local tomorrow."""
    tomorrow = T0.astimezone(TZ).date() + timedelta(days=1)

    def pv(a, b):
        kwh, t = 0.0, a
        while t < b:
            u = min(t + timedelta(minutes=15), b)
            loc = t.astimezone(TZ)
            if loc.date() == tomorrow and 8 <= loc.hour < 16:
                kwh += tomorrow_kw * (u - t).total_seconds() / 3600
            t = u
        return kwh
    return pv


def test_the_level_is_let_go_on_the_evening_before_a_sunny_day():
    def run(tomorrow_kw, until=None):
        return make_plan(T0, TZ, OPTS, tariff("fixed", OPTS), [], flat(0.6), dull_then(tomorrow_kw), 60,
                         forecast_until=until, peak_shave=True)
    today = T0.astimezone(TZ).date()
    night = OPTS.night_start_time
    sunny = run(5.0)                                                     # 40 kWh tomorrow: refills the battery
    for s in sunny.steps:
        loc = (s.start + timedelta(hours=s.hours / 2)).astimezone(TZ)
        if loc.date() == today:
            assert s.hold == (0 if loc.time() >= night else 50), loc
    late = [s for s in sunny.steps if s.start.astimezone(TZ).date() == today and s.start.astimezone(TZ).time() >= night]
    assert late and min(s.soc_end for s in sunny.steps if s.start.astimezone(TZ).date() == today) < 50
    dull = run(0.0)                                                      # dull tomorrow too: held all evening
    assert all(s.hold == 50 for s in dull.steps)
    unknown = run(5.0, until=T0 + timedelta(hours=6))                    # tomorrow not forecast yet: held
    assert all(s.hold == 50 for s in unknown.steps if s.start.astimezone(TZ).date() == today)


# ---------------------------------------------------------------- controller
def step(action="self_use", hold=50.0):
    return Step(T0, 1.0, action, 60, 50, 0, 0, 0, 0, 27, 5, 0, hold=hold)


def rd(soc, load=0.6, pv=0.0):
    return Readings(T0, T0.astimezone(TZ), soc, load, pv, load, load, 51.2)


def go(st, soc, mode, age=1e9, **kw):
    return decide(st, rd(soc, **kw), OPTS, mode, "", 0, True, mode_age_s=age, can_shave=True)


def test_the_controller_holds_the_level_with_a_band():
    assert go(step(), 51, "self_use").command == "none"
    d = go(step(), 50, "self_use")
    assert d.command == "peak_shave" and d.reason.startswith("winter level")
    assert go(step("peak_shave"), 49, "peak_shave").command == "none"
    # once started, peak shaving lasts until WINTER_BAND above the level, whatever the plan says
    for soc in (50, 52, 54.9):
        assert go(step(), soc, "peak_shave").command == "none"
    assert go(step(), 50 + WINTER_BAND, "peak_shave").command == "self_use"
    # without the switch, without a level: as before
    assert decide(step(), rd(45), OPTS, "self_use", "", 0, True, can_shave=False).command == "none"
    assert go(step(hold=0.0), 45, "self_use").command == "none"
    # a planned charge, the car and the fill switch still win
    assert go(step("charge"), 45, "peak_shave").command == "charge"
    car = decide(step(), rd(45, load=5.4), OPTS, "self_use", "", 0, True, can_shave=True, car_kw=5.0)
    assert car.command == "peak_shave" and "car" in car.reason


def test_no_switch_within_the_dwell_time():
    assert go(step(), 49, "self_use", age=60).command == "none"                    # just switched: waits
    assert go(step(), 49, "self_use", age=WINTER_DWELL_S).command == "peak_shave"
    assert go(step(), 54, "peak_shave", age=60).command == "none"                 # (still in the band)
    assert go(step(), 49, "peak_shave", age=60).command == "none"


def test_a_wobbling_soc_does_not_flip_the_inverter():
    mode, age, changes = "self_use", 1e9, 0
    for i, soc in enumerate([52, 51, 50, 49, 50, 51, 52, 53, 52, 51, 50, 49, 50, 52, 53, 54, 53, 52] * 3):
        d = go(step("peak_shave" if soc <= 50 else "self_use"), soc, mode, age=age, pv=0.3 * (i % 2))
        if d.command in ("self_use", "peak_shave") and d.command != mode:
            mode, age, changes = d.command, 0.0, changes + 1
        age += 120
    assert changes == 1 and mode == "peak_shave"


# ---------------------------------------------------------------- the whole app
async def run(tmp_path, until, soc=60, load=None, pv=None, **opts):
    fake = FakeHA(T0, soc=soc, load=load or (lambda t: 0.8), pv=pv or (lambda t: 0.0), peak_shaving=True)
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(battery_min_soc=10, winter_hold_soc=50, car_min_kw=3.5, **opts), fake,
              data_dir=str(tmp_path / "d"), clock=lambda: fake.now)
    assert await app.setup()
    while fake.now < until:
        await app.step()
        fake.advance(timedelta(seconds=30))
    return app, fake


def switches(fake):
    return [s for _, s, d in fake.calls if s in ("turn_on", "turn_off") and "peak" in str(d.get("entity_id"))]


@pytest.mark.slow
async def test_the_house_stops_at_the_level_and_nothing_buys_it_back(tmp_path):
    app, fake = await run(tmp_path, utc(2026, 1, 14, 23, 0))
    assert 47 <= fake.soc <= 51                                       # stopped at the level
    assert app.solis.mode == "peak_shave"
    assert not any("charge" in e["command"].lower() for e in app.solis.log)
    assert all(r.kwh["import"] * 4 <= 1.0 for r in app.records[-8:])  # the house runs on the grid
    assert len(switches(fake)) <= 3
    assert app.winter_days and all(app.winter_days.values())
    # saved with the state: a restart keeps the decision
    app.save_state()
    again = App(app.o, fake, data_dir=str(tmp_path / "d"), clock=lambda: fake.now)
    assert again.load_state() and again.winter_days == app.winter_days


@pytest.mark.slow
async def test_the_car_uses_the_winter_level(tmp_path):
    car = utc(2026, 1, 14, 19, 0)
    load = lambda t: 0.4 + (5.0 if car <= t < car + timedelta(hours=3) else 0.0)     # noqa: E731
    app, fake = await run(tmp_path, utc(2026, 1, 14, 21, 0), soc=52, load=load)
    assert app.car_state.charging and fake.soc < 40                   # below the level, down to the minimum
    cov = app.status()["car"]["coverage"]
    assert cov["winter"]["soc"] == 50 and cov["winter"]["pct"] > 0


@pytest.mark.slow
async def test_passing_clouds_do_not_flip_the_inverter(tmp_path):
    sun = lambda t: 1.2 if (t.minute // 10) % 2 else 0.0               # noqa: E731
    app, fake = await run(tmp_path, utc(2026, 1, 15, 2, 0), soc=53, pv=sun)
    assert len(switches(fake)) <= 4


def test_the_replay_keeps_the_level():
    recs = day_of_quarters()
    load = LoadProfile()
    load.learn_records(recs, TZ)
    out = {o.winter_hold_soc: replay_day(recs, "fixed", o, TZ, [], load, lambda a, b: 0.0, 70.0, None, peak_shave=True)
           for o in (Options(battery_min_soc=10), OPTS)}
    assert out[50]["soc_end"] == pytest.approx(50, abs=1) and out[0]["soc_end"] < 45
    assert out[50]["grid_charged"] == pytest.approx(0, abs=0.05)
