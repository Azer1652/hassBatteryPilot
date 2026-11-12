"""How much of the car charges under the import limit, and the car leaving the peak buffer alone."""
from datetime import timedelta

import pytest
from fake_ha import TZ, FakeHA, utc
from test_car import day_of_quarters

from pilot.app import App
from pilot.car import CAR_EFF, coverage
from pilot.compare import replay_day
from pilot.config import Options
from pilot.load import LoadProfile
from pilot.simulate import Battery, step

CONTROL = "input_boolean.battery_pilot_control"
SHAVE_SOC = "number.solis_peak_baseline_soc"
T0 = utc(2026, 1, 14, 20, 0)


def flat(kw):
    return lambda a, b: kw * (b - a).total_seconds() / 3600


NO_SUN = flat(0.0)


# ---------------------------------------------------------------- the estimate
def test_a_charger_under_the_limit_needs_no_battery():
    o = Options(car_battery_kwh=40)
    c = coverage(o, 50, 1.5, 25, T0, flat(0.4), NO_SUN)
    assert c.whole and c.pct == 100 and c.kwh == 40
    assert c.hours == pytest.approx(40 / CAR_EFF / 1.5, abs=0.01)


def test_the_car_charges_until_the_battery_reaches_its_floor():
    o = Options(car_battery_kwh=60)
    c = coverage(o, 65, 5.0, 25, T0, flat(0.4), NO_SUN)
    eta = o.discharge_efficiency
    hours = (65 - 25) / 100 * o.battery_capacity_kwh * eta / (5.0 + 0.4 - o.import_limit_kw)
    assert not c.whole
    assert c.hours == pytest.approx(hours, abs=0.03)
    assert c.pct == pytest.approx(5.0 * hours * CAR_EFF / 60 * 100, abs=1)
    full = coverage(o, 100, 5.0, 25, T0, flat(0.4), NO_SUN)
    assert full.pct > c.pct and full.soc == 100
    lower = coverage(o, 65, 5.0, 10, T0, flat(0.4), NO_SUN)        # no peak buffer: down to the minimum
    assert lower.pct > c.pct


def test_sun_and_a_small_car_stretch_it():
    o = Options(car_battery_kwh=60)
    dark = coverage(o, 65, 5.0, 25, T0, flat(0.4), NO_SUN)
    sunny = coverage(o, 65, 5.0, 25, T0, flat(0.4), flat(1.5))
    assert sunny.pct > dark.pct
    small = coverage(Options(car_battery_kwh=8), 65, 5.0, 25, T0, flat(0.4), NO_SUN)
    assert small.whole and small.pct == 100


def test_at_the_floor_or_too_fast_for_the_battery_the_limit_goes_at_once():
    o = Options(car_battery_kwh=60)
    assert coverage(o, 25, 5.0, 25, T0, flat(0.4), NO_SUN).pct == 0
    too_fast = 11.0 + 0.4 - o.import_limit_kw > o.max_discharge_kw * o.discharge_efficiency
    assert too_fast and coverage(o, 90, 11.0, 25, T0, flat(0.4), NO_SUN).pct == 0


def test_the_simulator_keeps_the_floor_while_peak_shaving():
    b = Battery()
    res = step(b, 26, "peak_shave", 26, 5.4 * 0.25, 0.0, 0.25, shave_floor=25)
    assert res.soc == pytest.approx(25)
    assert step(b, 26, "peak_shave", 26, 5.4 * 0.25, 0.0, 0.25).soc < 25        # (default: the minimum)


# ---------------------------------------------------------------- the whole app
def car_load(start, hours, kw=5.0, house=0.4):
    return lambda t: house + (kw if start <= t < start + timedelta(hours=hours) else 0.0)


async def evening(tmp_path, soc, run_until, start=utc(2026, 1, 14, 19, 0), **opts):
    """The car charges at 5 kW from 20:00 for 3 hours."""
    fake = FakeHA(start, soc=soc, load=car_load(T0, 3), peak_shaving=True)
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(car_min_kw=3.5, **opts), fake, data_dir=str(tmp_path / "d"), clock=lambda: fake.now)
    assert await app.setup()
    while fake.now < run_until:
        await app.step()
        fake.advance(timedelta(seconds=30))
    return app, fake


@pytest.mark.slow
async def test_the_car_leaves_the_peak_buffer_for_the_house(tmp_path):
    app, fake = await evening(tmp_path, 45, utc(2026, 1, 14, 22, 30), peak_buffer_pct=15)
    level = Options().battery_min_soc + 15
    sets = [d["value"] for _, s, d in fake.calls if s == "set_value" and d.get("entity_id") == SHAVE_SOC]
    assert level in sets
    assert fake.soc >= level - 1                                   # the car did not take the buffer
    assert app.status()["car"]["charging"]
    # the car stops: the baseline goes back to the minimum
    while fake.now < utc(2026, 1, 14, 23, 30):
        await app.step()
        fake.advance(timedelta(seconds=30))
    assert not app.car_state.charging
    if app.solis.mode == "peak_shave":
        assert float(fake.s[SHAVE_SOC]["state"]) == app.shave_level == Options().battery_min_soc


@pytest.mark.slow
async def test_a_car_below_the_buffer_does_not_make_the_inverter_buy(tmp_path):
    app, fake = await evening(tmp_path, 18, utc(2026, 1, 14, 21, 0), start=utc(2026, 1, 14, 19, 58),
                              peak_buffer_pct=15)
    floor = app._car_floor
    assert app.car_state.charging and floor < 25                   # the SOC the car found
    assert float(fake.s[SHAVE_SOC]["state"]) == floor
    assert fake.soc <= floor + 1                                   # not refilled from the grid


async def test_without_a_buffer_the_car_uses_the_battery_to_the_minimum(tmp_path):
    app, fake = await evening(tmp_path, 60, utc(2026, 1, 14, 20, 30))
    assert app.car_state.charging and app.shave_level == Options().battery_min_soc
    assert float(fake.s[SHAVE_SOC]["state"]) == Options().battery_min_soc


def test_the_defaults_are_an_eqb_250_plus_on_a_5_kw_charger():
    assert (Options().car_battery_kwh, Options().car_charge_kw) == (70.5, 5.0)


async def test_the_page_gets_the_estimate(tmp_path):
    app, _ = await evening(tmp_path, 60, utc(2026, 1, 14, 19, 5), car_battery_kwh=60, car_charge_kw=5.0,
                           peak_buffer_pct=15)
    cov = app.status()["car"]["coverage"]
    assert cov["charger_kw"] == 5.0 and not cov["measured"] and cov["floor_soc"] == 25 and cov["buffer"]
    assert 0 < cov["now"]["pct"] < cov["full"]["pct"] < 100 and cov["now"]["soc"] == 60
    assert 0 < cov["free_kw"] < 2.3
    # while the car charges, its measured power counts
    while app.clock() < utc(2026, 1, 14, 20, 10):
        await app.step()
        app.ha.advance(timedelta(seconds=30))
    cov = app.status()["car"]["coverage"]
    assert cov["measured"] and cov["charger_kw"] == pytest.approx(5.0, abs=0.1)


async def test_no_estimate_without_the_size_and_a_reason_without_the_power(tmp_path):
    app, _ = await evening(tmp_path, 60, utc(2026, 1, 14, 19, 2), car_battery_kwh=0)
    assert "coverage" not in app.status()["car"]
    app.o.car_battery_kwh, app.o.car_charge_kw = 60, 0
    car = app.status()["car"]
    assert car["coverage"] is None and "charging power" in car["coverage_why"]


def test_the_replay_keeps_the_buffer_while_the_car_charges():
    recs = day_of_quarters(car_from=4, car_hours=2.5)
    load = LoadProfile()
    load.learn_records(day_of_quarters(), TZ)
    out = {}
    for buffer in (0, 40):
        o = Options(peak_buffer_pct=buffer)
        out[buffer] = replay_day(recs, "fixed", o, TZ, [], load, lambda a, b: 0.0, 80.0, None, peak_shave=True)
    assert out[0]["peak_kw"] <= Options().import_limit_kw + 0.05
    assert out[40]["peak_kw"] > out[0]["peak_kw"] + 1      # the car stopped at the buffer: the grid took the rest
