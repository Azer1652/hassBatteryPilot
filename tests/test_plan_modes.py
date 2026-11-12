"""The plan chart shows what the inverter does: `controller.expected_modes` runs the controller's own
rules over the plan, so bands, waits, the car and the fill switch are in the chart too."""
from datetime import timedelta

import pytest
from fake_ha import TZ, FakeHA, utc

from pilot.app import App
from pilot.config import Options
from pilot.controller import expected_modes
from pilot.planner import Step

CONTROL = "input_boolean.battery_pilot_control"
T0 = utc(2026, 1, 15, 8, 0)


def st(i, action, soc0, soc1, load=0.5, pv=0.0, hold=0.0):
    return Step(T0 + timedelta(hours=i), 1.0, action, soc0, soc1, 0, 0, load, pv, 27, 5, 0, hold=hold)


def modes(steps, o, start="self_use", **kw):
    return [m for m, _ in expected_modes(steps, o, TZ, start, True, **kw)]


def test_below_the_winter_level_solar_hours_stay_peak_shaving():
    o = Options(battery_min_soc=10, winter_hold_soc=80)
    steps = [st(0, "peak_shave", 57, 57, hold=80),
             st(1, "self_use", 57, 60, load=0.4, pv=1.2, hold=80),     # solar charges: the plan says self-use
             st(2, "self_use", 60, 83, load=0.4, pv=3.0, hold=80),
             st(3, "self_use", 83, 86, load=0.4, pv=1.0, hold=80),     # in the band: still peak shaving
             st(4, "self_use", 86, 84, load=0.6, hold=80)]             # 5 points above: self-use
    got = expected_modes(steps, o, TZ, "peak_shave", True)
    assert [m for m, _ in got] == ["peak_shave", "peak_shave", "peak_shave", "peak_shave", "self_use"]
    assert "winter level" in got[1][1]
    # without the switch the plan's own labels hold (self-use and its peak_shave are not possible)
    assert [m for m, _ in expected_modes(steps[1:], o, TZ, "self_use", False)] == ["self_use"] * 4


def test_the_peak_buffer_band_shows_too():
    o = Options(battery_min_soc=10, peak_buffer_pct=10)
    steps = [st(0, "self_use", 20, 21, load=0.4, pv=1.0), st(1, "self_use", 21, 22, load=0.4, pv=1.0),
             st(2, "self_use", 22, 21)]
    assert modes(steps, o, "peak_shave") == ["peak_shave", "peak_shave", "self_use"]


def test_the_car_now_and_the_fill_switch():
    o = Options()
    steps = [st(0, "self_use", 60, 50, load=5.4), st(1, "self_use", 50, 48)]
    assert modes(steps, o, car_kw=5.0) == ["peak_shave", "self_use"]       # the car's future is not known
    full = [st(0, "self_use", 50, 49), st(1, "self_use", 100, 100)]
    assert modes(full, o, fill=True) == ["charge", "peak_shave"]


def test_a_charge_without_room_and_a_hold_without_surplus_are_self_use():
    o = Options()
    got = expected_modes([st(0, "charge", 40, 60, load=4.0)], o, TZ, "self_use", True)
    assert got[0][0] == "self_use" and "room" in got[0][1]
    assert modes([st(0, "charge", 40, 60, load=0.3)], o) == ["charge"]
    assert modes([st(0, "solar_hold", 60, 60, load=0.5, pv=0.6)], o) == ["self_use"]
    assert modes([st(0, "solar_hold", 60, 60, load=0.5, pv=2.0)], o) == ["solar_hold"]


@pytest.mark.slow
async def test_the_chart_matches_what_the_inverter_does(tmp_path):
    sun = lambda t: max(0.0, 1.5 - abs((t.astimezone(TZ).hour + t.minute / 60) - 12.5) * 0.5)   # noqa: E731
    fake = FakeHA(utc(2026, 1, 14, 17, 0), soc=90, load=lambda t: 1.0, pv=sun, peak_shaving=True)
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(battery_min_soc=10, winter_hold_soc=70, peak_buffer_pct=5), fake,
              data_dir=str(tmp_path / "d"), clock=lambda: fake.now)
    assert await app.setup()
    checks = misses = 0
    while fake.now < utc(2026, 1, 15, 17, 0):
        await app.step()
        if fake.now.minute % 15 == 0 and fake.now.second == 0:
            first = app.status()["plan"]["steps"][0]
            app._seen = getattr(app, "_seen", set()) | {app.solis.mode}
            assert first["mode"] == app.solis.mode                 # now: what the inverter is in
            expected = app.plan.step_at(fake.now + timedelta(minutes=20))
            later = expected.mode if expected else None
            checks += 1
            app._check = (fake.now + timedelta(minutes=20), later)
        if getattr(app, "_check", None) and fake.now >= app._check[0]:
            misses += app._check[1] != app.solis.mode
            app._check = None
        fake.advance(timedelta(seconds=30))
    assert app._seen == {"self_use", "peak_shave"}            # a run that goes through a switch
    assert checks > 80 and misses <= 4
    sensor = fake.published["sensor.battery_pilot_plan"]
    assert sensor["state"] == app.solis.mode
    assert all("mode" in h for h in sensor["attributes"]["next_hours"])
