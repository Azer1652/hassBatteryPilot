"""The electric car: seen in the house consumption, its peak shaved, and kept out of what is learned."""
from datetime import date, datetime, timedelta, timezone

import pytest
from fake_ha import TZ, FakeHA, st, utc

from pilot.app import App
from pilot.car import ChargerPowerSource, LoadStepSource, NoCar, make_source
from pilot.compare import replay_day
from pilot.config import Options
from pilot.controller import Readings, decide
from pilot.load import LoadProfile
from pilot.meter import QuarterRecord
from pilot.planner import Step

T0 = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)
CONTROL = "input_boolean.battery_pilot_control"
SHAVE = "switch.solis_peak_shaving_mode"


def feed(src, loads, t=T0, step_s=10):
    """Feed (seconds, kW) stretches of house load; returns the states seen and the end time."""
    out = []
    for seconds, kw in loads:
        for _ in range(int(seconds / step_s)):
            out.append(src.update(t, kw))
            t += timedelta(seconds=step_s)
    return out, t


# ---------------------------------------------------------------- detection
def test_a_jump_that_stays_is_the_car_and_ends_when_it_is_gone():
    src = LoadStepSource(3.5)
    seen, t = feed(src, [(600, 0.4), (170, 5.4)])
    assert not any(s.charging for s in seen)                    # not yet: three minutes
    seen, t = feed(src, [(60, 5.4)], t)
    assert seen[-1].charging and seen[-1].power_kw == pytest.approx(5.0, abs=0.05)
    seen, t = feed(src, [(1800, 5.9)], t)                       # the house uses a little more meanwhile
    assert seen[-1].charging and seen[-1].power_kw == pytest.approx(5.5, abs=0.05)
    seen, t = feed(src, [(110, 0.4)], t)
    assert seen[-1].charging                                    # not gone long enough yet
    seen, t = feed(src, [(60, 0.4)], t)
    assert not seen[-1].charging and seen[-1].power_kw == 0.0


def test_a_kettle_or_the_hob_is_not_the_car():
    src = LoadStepSource(3.5)
    seen, t = feed(src, [(600, 0.4), (60, 4.5), (300, 0.4)])    # a short burst, however high
    assert not any(s.charging for s in seen)
    seen, t = feed(src, [(1800, 2.8)], t)                       # cooking: long, but below the threshold
    assert not any(s.charging for s in seen)


def test_a_house_that_slowly_uses_more_is_not_the_car():
    src = LoadStepSource(3.5)
    loads = [(300, 0.4 + 0.3 * i) for i in range(16)]           # up to 4.9 kW over 80 minutes
    seen, _ = feed(src, loads)
    assert not any(s.charging for s in seen)


def test_lost_readings_end_a_session_after_a_few_minutes():
    src = LoadStepSource(3.5)
    seen, t = feed(src, [(600, 0.4), (300, 5.4)])
    assert seen[-1].charging
    assert src.update(t + timedelta(seconds=60), None).charging            # a short gap keeps it
    assert not src.update(t + timedelta(seconds=400), None).charging
    assert not src.update(T0, None).charging


def test_the_charger_sensor_is_used_as_it_is():
    src = ChargerPowerSource()
    assert not src.update(T0, 6.0, 0.02).charging                           # standby
    s = src.update(T0, 0.4, 4.8)
    assert s.charging and s.power_kw == 4.8
    assert src.update(T0 + timedelta(seconds=30), 0.4, None).charging       # one missed reading
    assert not src.update(T0 + timedelta(minutes=5), 0.4, None).charging


def test_the_source_follows_the_settings():
    assert isinstance(make_source(Options(), {}), NoCar)                    # off by default
    assert isinstance(make_source(Options(car_min_kw=3.5), {}), LoadStepSource)
    assert isinstance(make_source(Options(), {"car_charger_power": "sensor.x"}), ChargerPowerSource)
    seen, _ = feed(make_source(Options(), {}), [(600, 0.4), (600, 9.0)])
    assert not any(s.charging for s in seen)
    state = seen[0]
    assert state.soc is None and state.plugged is None and state.target_soc is None   # for a car integration, later


# ---------------------------------------------------------------- controller
def step(action, soc_end=30):
    return Step(T0.replace(minute=0), 1.0, action, 30, soc_end, 0, 0, 0, 0, 27, 5, 0)


def rd(load=5.4, pv=0.0, soc=60):
    return Readings(T0, T0.astimezone(TZ), soc, load, pv, load, load, 51.2)


def test_the_car_puts_the_inverter_in_peak_shaving_whatever_the_plan_says():
    o = Options()
    for action in ("self_use", "charge", "discharge", "solar_hold"):
        d = decide(step(action, 60), rd(), o, "self_use", "", 0, True, car_kw=5.0, can_shave=True)
        assert d.command == "peak_shave", action
    assert decide(None, rd(), o, "self_use", "", 0, True, car_kw=5.0, can_shave=True).command == "peak_shave"
    assert decide(step("self_use"), rd(), o, "peak_shave", "", 0, True, car_kw=5.0, can_shave=True).command == "none"
    assert decide(step("self_use"), rd(), o, "failed", "", 0, True, car_kw=5.0, can_shave=True).command == "peak_shave"


def test_no_switch_no_car_or_enough_sun_follow_the_plan():
    o = Options()
    assert decide(step("self_use"), rd(), o, "self_use", "", 0, True, car_kw=5.0, can_shave=False).command == "none"
    assert decide(step("self_use"), rd(), o, "self_use", "", 0, True, car_kw=0.0, can_shave=True).command == "none"
    sunny = rd(load=5.4, pv=4.0)                                 # 1.4 kW from the grid: under the limit
    assert decide(step("self_use"), sunny, o, "self_use", "", 0, True, car_kw=5.0, can_shave=True).command == "none"
    # once shaving, a moment of sun does not switch it back and forth
    assert decide(step("self_use"), sunny, o, "peak_shave", "", 0, True, car_kw=5.0, can_shave=True).command == "none"
    # the car is gone: back to the plan
    assert decide(step("self_use"), rd(load=0.4), o, "peak_shave", "", 0, True, car_kw=0.0, can_shave=True).command == "self_use"
    # control off always wins
    assert decide(step("self_use"), rd(), o, "peak_shave", "", 0, False, car_kw=5.0, can_shave=True).command == "self_use"


# ---------------------------------------------------------------- learning
def day_of_quarters(car_from=None, car_hours=0.0, car_kw=5.0):
    start = datetime(2026, 1, 14, 0, tzinfo=TZ).astimezone(timezone.utc)
    recs = []
    for q in range(96):
        t = start + timedelta(minutes=15 * q)
        car = car_kw / 4 if car_from is not None and car_from <= q < car_from + car_hours * 4 else 0.0
        recs.append(QuarterRecord(t, {"import": 0.0, "export": 0.0, "load": 0.5 / 4 + car, "pv": 0.0},
                                  {"import": 1.0, "export": 1.0, "load": 1.0, "pv": 1.0}, 50.0, car=car))
    return recs


def test_the_car_is_not_learned_as_house_consumption_or_as_a_usual_peak():
    plain, with_car = LoadProfile(), LoadProfile()
    plain.learn_records(day_of_quarters(), TZ)
    with_car.learn_records(day_of_quarters(car_from=80, car_hours=2.5), TZ)       # 20:00-22:30
    assert with_car.to_json() == plain.to_json()
    # a record without the flag (older data, or detection off) is learned as before
    old = LoadProfile()
    recs = day_of_quarters(car_from=80, car_hours=2)
    for r in recs:
        r.car = 0.0
    old.learn_records(recs, TZ)
    assert old.kw_at(2, 20) == pytest.approx(5.5)
    rec = QuarterRecord.from_json(day_of_quarters(car_from=0, car_hours=1)[0].to_json())
    assert rec.car == pytest.approx(1.25) and rec.house_kwh == pytest.approx(0.125)
    assert "car" not in day_of_quarters()[0].to_json()


# ---------------------------------------------------------------- the whole app
def car_load(start, hours, kw=5.0, house=0.4):
    return lambda t: house + (kw if start <= t < start + timedelta(hours=hours) else 0.0)


async def session(tmp_path, name, soc=60, peak_shaving=True, **opts):
    """A January evening: the car charges at 5 kW from 20:00 to 22:00 (local 21:00-23:00)."""
    begin = utc(2026, 1, 14, 20, 0)
    fake = FakeHA(utc(2026, 1, 14, 19, 0), soc=soc, load=car_load(begin, 2), peak_shaving=peak_shaving)
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(**opts), fake, data_dir=str(tmp_path / name), clock=lambda: fake.now)
    assert await app.setup()
    highest = [0.0]

    async def run(end):
        while fake.now < end:
            await app.step()
            fake.advance(timedelta(seconds=30))

    await run(utc(2026, 1, 14, 22, 30))
    highest[0] = max((r.kwh["import"] * 4 for r in app.records), default=0.0)
    return app, fake, highest[0], run


async def test_the_cars_peak_is_shaved_while_the_battery_lasts(tmp_path):
    app, fake, peak, run = await session(tmp_path, "on", car_min_kw=3.5)
    texts = [e["text"] for e in app.events]
    assert any(t.startswith("Car charging detected (about 5.0 kW): peak shaving") for t in texts)
    assert any(t.startswith("Car charging ended: ") for t in texts)
    assert peak <= Options().import_limit_kw + 0.15              # the grid stayed at the limit
    assert fake.soc > 12                                         # and the battery lasted the session
    assert app.solis.mode != "peak_shave" or app.plan.step_at(fake.now).action == "peak_shave"   # back on the plan
    car_kwh = sum(r.car for r in app.records)
    assert car_kwh == pytest.approx(10.0, abs=0.6)               # 2 hours at 5 kW (minus the minutes to detect it)
    status = app.status()["car"]
    assert status["enabled"] and not status["charging"] and status["month_kwh"] == pytest.approx(car_kwh, abs=0.1)
    assert status["source"] == "house consumption" and status["soc"] is None

    # the same evening without detection: the battery feeds the whole car and is empty before it ends
    app2, fake2, peak2, _ = await session(tmp_path, "off")
    assert fake2.soc <= Options().battery_min_soc + 1
    assert peak2 > 4.0                                           # the grid then takes the car: a capacity peak
    assert not app2.status()["car"]["enabled"] and sum(r.car for r in app2.records) == 0


async def test_during_a_session_the_page_and_the_sensor_say_so(tmp_path):
    begin = utc(2026, 1, 14, 20, 0)
    fake = FakeHA(utc(2026, 1, 14, 19, 0), soc=70, load=car_load(begin, 2), peak_shaving=True)
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(car_min_kw=3.5), fake, data_dir=str(tmp_path / "d"), clock=lambda: fake.now)
    assert await app.setup()
    while fake.now < utc(2026, 1, 14, 20, 30):
        await app.step()
        fake.advance(timedelta(seconds=30))
    car = app.status()["car"]
    assert car["charging"] and car["shaving"] and car["power_kw"] == pytest.approx(5.0, abs=0.1)
    assert car["session_kwh"] == pytest.approx(2.2, abs=0.4)
    assert fake.mode() == "peak_shave" and fake.import_kw == pytest.approx(2.3, abs=0.1)
    await app.publish()
    sensor = fake.published["sensor.battery_pilot_car_power"]
    assert float(sensor["state"]) == pytest.approx(5.0, abs=0.1) and sensor["attributes"]["peak_shaving"] is True
    first = app.plan.steps[0]
    assert first.load_kwh / first.hours > 3.0                    # the plan counts the car for now
    # every way out ends the peak shaving
    fake.s[CONTROL]["state"] = "off"
    for _ in range(4):
        await app.step()
        fake.advance(timedelta(seconds=30))
    assert fake.s[SHAVE]["state"] == "off"


async def test_without_the_peak_shaving_switch_the_car_is_only_measured(tmp_path):
    app, fake, peak, _ = await session(tmp_path, "noswitch", peak_shaving=False, car_min_kw=3.5)
    assert any("peak-shaving switch was not found" in e["text"] for e in app.events)
    assert sum(r.car for r in app.records) == pytest.approx(10.0, abs=0.6)
    assert not [d for dom, _, d in fake.calls if "peak" in str(d.get("entity_id", ""))]
    app.load.learn_records(app.records, TZ)
    assert app.load.kw_at(2, 21) < 1.0                           # Wednesday 21:00: the house, not the car


async def test_a_charger_sensor_replaces_the_guess(tmp_path):
    fake = FakeHA(utc(2026, 1, 14, 19, 0), soc=70, load=lambda t: 5.4, peak_shaving=True)
    fake.s["sensor.wallbox_power"] = st(4800, "W")
    fake.s[CONTROL]["state"] = "on"
    o = Options()
    o.entities = {"car_charger_power": "sensor.wallbox_power"}
    app = App(o, fake, data_dir=str(tmp_path / "d"), clock=lambda: fake.now)
    assert await app.setup()
    await app.step()
    car = app.status()["car"]
    assert car["charging"] and car["power_kw"] == 4.8 and car["source"] == "charger sensor"
    fake.advance(timedelta(seconds=30))
    await app.step()
    assert fake.mode() == "peak_shave"                           # at once: no three minutes of doubt


# ---------------------------------------------------------------- the nightly replay
def test_the_replay_shaves_the_car_too():
    recs = day_of_quarters(car_from=4, car_hours=2.5)            # 01:00-03:30 local
    load = LoadProfile()
    load.learn_records(day_of_quarters(), TZ)
    o = Options()
    args = (recs, "fixed", o, TZ, [], load, lambda a, b: 0.0, 80.0, None)
    shaved = replay_day(*args, peak_shave=True)
    plain = replay_day(*args, peak_shave=False)
    assert shaved["peak_kw"] <= o.import_limit_kw + 0.05
    assert plain["peak_kw"] > 4.0                                # the battery ran out before the car was done
    assert date(2026, 1, 14) == recs[0].start.astimezone(TZ).date()
