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


def feed_usual(src, seconds, kw, usual, t=T0, step_s=10):
    out = []
    for _ in range(int(seconds / step_s)):
        out.append(src.update(t, kw, usual_kw=usual))
        t += timedelta(seconds=step_s)
    return out, t


def test_a_car_that_already_charges_at_the_start_is_seen_against_the_usual_load():
    src = LoadStepSource(3.5)
    seen, t = feed_usual(src, 170, 5.4, usual=0.5)              # the first reading is the car already
    assert not any(s.charging for s in seen)
    seen, t = feed_usual(src, 30, 5.4, usual=0.5, t=t)
    assert seen[-1].charging and seen[-1].power_kw == pytest.approx(4.9, abs=0.05)
    seen, t = feed_usual(src, 180, 0.5, usual=0.5, t=t)
    assert not seen[-1].charging
    # without the usual load it stays unseen: the car is taken for the house
    seen, _ = feed(LoadStepSource(3.5), [(1800, 5.4)])
    assert not any(s.charging for s in seen)


def test_the_usual_load_is_only_used_without_readings_before():
    src = LoadStepSource(3.5)
    seen, t = feed_usual(src, 600, 2.0, usual=0.5)              # the house uses more than usual tonight
    seen, t = feed_usual(src, 600, 4.6, usual=0.5, t=t)         # above usual + 3.5, but no jump from 2.0
    assert not any(s.charging for s in seen)
    seen, t = feed_usual(src, 300, 8.5, usual=0.5, t=t)         # a jump from what the house used: the car
    assert seen[-1].charging


def test_a_session_resumed_after_a_restart_ends_when_the_car_stops():
    src = LoadStepSource(3.5)
    assert src.resume(0.4, 5.0).charging
    seen, t = feed(src, [(600, 5.4)])
    assert all(s.charging for s in seen) and seen[-1].power_kw == pytest.approx(5.0)
    seen, _ = feed(src, [(180, 0.4)], t)
    assert not seen[-1].charging
    assert not LoadStepSource(3.5).resume(None, 5.0).charging


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


@pytest.mark.slow
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


async def test_a_restart_while_the_car_charges_does_not_lose_it(tmp_path):
    begin = utc(2026, 1, 14, 20, 0)
    fake = FakeHA(utc(2026, 1, 14, 20, 30), soc=70, load=car_load(begin, 2), peak_shaving=True)
    fake.s[CONTROL]["state"] = "on"

    async def run(app, minutes):
        end = fake.now + timedelta(minutes=minutes)
        while fake.now < end:
            await app.step()
            fake.advance(timedelta(seconds=30))

    # the app starts while the car already charges: no jump to see, the usual load is the level
    app = App(Options(car_min_kw=3.5), fake, data_dir=str(tmp_path / "d"), clock=lambda: fake.now)
    assert await app.setup()
    await run(app, 5)
    assert app.car_state.charging and app.car_state.power_kw > 3.5
    assert fake.mode() == "peak_shave"
    # a restart of the app: the session goes on (one session, not a second "detected")
    await app.shutdown()
    fake.advance(timedelta(minutes=1))
    app2 = App(Options(car_min_kw=3.5), fake, data_dir=str(tmp_path / "d"), clock=lambda: fake.now)
    assert await app2.setup()
    assert app2.car_state.charging
    await run(app2, 90)
    texts = [e["text"] for e in app2.events]
    assert any(t.startswith("Car charging continues after the restart") for t in texts)
    assert not any(t.startswith("Car charging detected") for t in texts)
    assert any(t.startswith("Car charging ended: ") for t in texts) and not app2.car_state.charging


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


# ---------------------------------------------------------------- the fill switch
FILL = "input_boolean.battery_pilot_fill_battery_for_car"


def test_the_fill_switch_charges_to_full_under_the_limit_and_then_keeps_the_battery():
    o = Options()
    calm = rd(load=0.5, soc=40)
    d = decide(step("discharge", 20), calm, o, "self_use", "", 0, True, fill=True)
    assert d.command == "charge" and d.amps >= 15 and d.reason.startswith("fill switch")
    assert decide(None, calm, o, "self_use", "", 0, True, fill=True).command == "charge"       # also without a plan
    # the house takes the whole limit: no charging, the battery helps as usual
    assert decide(step("self_use"), rd(load=2.6, soc=40), o, "self_use", "", 0, True, fill=True).command == "none"
    assert decide(step("self_use"), rd(load=3.0, soc=40), o, "charge", "", 20, True, fill=True).command == "self_use"
    # full: kept with peak shaving when the inverter has it, and charged again only below 95 %
    full = rd(load=0.5, soc=100)
    assert decide(step("self_use"), full, o, "charge", "", 10, True, can_shave=True, fill=True).command == "peak_shave"
    assert decide(step("self_use"), rd(load=0.5, soc=96), o, "peak_shave", "", 0, True, can_shave=True,
                  fill=True).command == "none"
    assert decide(step("self_use"), rd(load=0.5, soc=94), o, "peak_shave", "", 0, True, can_shave=True,
                  fill=True).command == "charge"
    # without that switch: held at the minimum charge current
    hold = decide(step("self_use"), full, o, "charge", T0.astimezone(TZ).strftime("%H:00:00"), 30, True, fill=True)
    assert (hold.command, hold.amps) == ("current", 10)
    # the car, control off and the switch off each win over it
    assert decide(step("self_use"), rd(soc=40), o, "self_use", "", 0, True, car_kw=5.0, can_shave=True,
                  fill=True).command == "peak_shave"
    assert decide(step("self_use"), calm, o, "self_use", "", 0, False, fill=True).command == "none"
    assert decide(step("self_use"), calm, o, "charge", "", 20, True, fill=False).command == "self_use"


async def fill_day(tmp_path, peak_shaving=True):
    """A January afternoon at 40 %, the switch on at 14:00, the car from 20:00 to 22:00."""
    fake = FakeHA(utc(2026, 1, 14, 14, 0), soc=40, load=car_load(utc(2026, 1, 14, 20, 0), 2),
                  peak_shaving=peak_shaving)
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(car_min_kw=3.5), fake, data_dir=str(tmp_path / "d"), clock=lambda: fake.now)
    assert await app.setup()

    async def run(end):
        while fake.now < end:
            await app.step()
            fake.advance(timedelta(seconds=30))

    return app, fake, run


@pytest.mark.slow
async def test_the_fill_switch_fills_the_battery_before_the_car_and_ends_with_it(tmp_path):
    app, fake, run = await fill_day(tmp_path)
    await app.set_fill(True)
    await run(utc(2026, 1, 14, 14, 30))
    car = app.status()["car"]
    assert car["fill"] and car["fill_state"] == "filling" and car["fill_until"].startswith("2026-01-15T13:0")
    assert fake.mode() == "charge"
    await run(utc(2026, 1, 14, 19, 55))
    assert fake.soc >= 95 and app.status()["car"]["fill_state"] == "holding"       # full, and kept
    assert fake.mode() == "peak_shave"
    await run(utc(2026, 1, 14, 22, 30))
    limit = Options().import_limit_kw
    assert max(r.kwh["import"] * 4 for r in app.records) <= limit + 0.15           # never above the limit
    assert fake.soc > 40                                         # the car's 6 kWh above the limit came from a full battery
    assert fake.s[FILL]["state"] == "off" and not app.fill_on    # the car has charged: the switch is off again
    assert any("turned off, the car has charged" in e["text"] for e in app.events)
    assert app.status()["car"]["fill"] is False and app.fill_since == ""


@pytest.mark.slow
async def test_the_fill_switch_ends_after_a_day_or_by_hand_and_needs_control(tmp_path):
    app, fake, run = await fill_day(tmp_path, peak_shaving=False)
    fake.load_fn = lambda t: 0.4                                    # the car never comes
    fake.s[CONTROL]["state"] = "off"
    fake.s[FILL]["state"] = "on"                                 # from a Home Assistant dashboard
    await run(utc(2026, 1, 14, 14, 10))
    assert app.status()["car"]["fill_state"] == "no_control" and fake.mode() == "self_use"
    fake.s[CONTROL]["state"] = "on"
    await run(utc(2026, 1, 14, 19, 55))
    assert fake.soc >= 99 and fake.mode() == "charge"            # no peak-shaving switch: held by the charge slot
    await run(utc(2026, 1, 15, 14, 5))
    assert fake.s[FILL]["state"] == "off"
    assert any("24 hours have passed" in e["text"] for e in app.events)
    # by hand: the plan is followed again at once
    await app.set_fill(True)
    await run(utc(2026, 1, 15, 14, 10))
    fake.s[FILL]["state"] = "off"
    await run(utc(2026, 1, 15, 14, 15))
    assert not app.fill_on and app.fill_since == ""
    assert any(e["text"].startswith("Fill the battery for the car: off") for e in app.events)


async def test_the_page_button_turns_the_helper_on_and_off(tmp_path, aiohttp_client):
    from pilot.web import make_web
    app, fake, run = await fill_day(tmp_path)
    client = await aiohttp_client(make_web(app, None))
    r = await client.post("/api/car_fill", json={"on": True})
    assert r.status == 200 and fake.s[FILL]["state"] == "on" and app.fill_on
    r = await client.post("/api/car_fill", json={"on": False})
    assert r.status == 200 and fake.s[FILL]["state"] == "off"
    assert (await client.post("/api/car_fill", json={"on": "yes"})).status == 400
    assert (await client.post("/api/car_fill", data="x")).status == 400
    outside = await aiohttp_client(make_web(app, {"172.30.32.2"}))
    assert (await outside.post("/api/car_fill", json={"on": True})).status == 403
    fake.fail = lambda *a: True
    assert (await client.post("/api/car_fill", json={"on": True})).status == 502


# ---------------------------------------------------------------- the peak buffer
def test_self_use_stops_at_the_peak_buffer_and_only_peaks_go_below_it():
    from pilot.simulate import Battery
    from pilot.simulate import step as sim
    o = Options(battery_min_soc=20, peak_buffer_pct=10)
    b = Battery.from_options(o)
    assert b.self_floor == 30 and Battery.from_options(Options()).self_floor == Options().battery_min_soc
    r = sim(b, 32, "self_use", 0, 1.0, 0.0, 1.0)                 # 1 kW for an hour: only the 2 points above 30
    assert r.soc == pytest.approx(30) and r.import_kwh > 0.6
    r = sim(b, 30, "self_use", 0, 0.5, 0.0, 0.25)                # under the limit at the buffer: all from the grid
    assert r.soc == 30 and r.import_kwh == pytest.approx(0.5)
    r = sim(b, 30, "self_use", 0, 1.25, 0.0, 0.25)               # 5 kW for a quarter: the grid at its limit
    assert r.import_kwh == pytest.approx(o.import_limit_kw * 0.25) and 20 < r.soc < 30
    r = sim(b, 20, "self_use", 0, 1.25, 0.0, 0.25)               # nothing left: the grid takes it
    assert r.soc == 20 and r.import_kwh == pytest.approx(1.25)
    assert sim(b, 25, "self_use", 0, 0.0, 1.0, 1.0).soc > 25     # solar fills it again


def test_the_controller_keeps_and_refills_the_peak_buffer():
    o = Options(battery_min_soc=20, peak_buffer_pct=10)
    calm = dict(load=0.5)
    assert decide(step("self_use"), rd(soc=31, **calm), o, "self_use", "", 0, True, can_shave=True).command == "none"
    d = decide(step("self_use"), rd(soc=30, **calm), o, "self_use", "", 0, True, can_shave=True)
    assert d.command == "peak_shave" and d.reason.startswith("peak buffer")
    # kept until solar has lifted it clear of the level
    assert decide(step("self_use"), rd(soc=31, **calm), o, "peak_shave", "", 0, True, can_shave=True).command == "none"
    assert decide(step("self_use"), rd(soc=33, **calm), o, "peak_shave", "", 0, True, can_shave=True).command == "self_use"
    # a peak took 3 points: charged back to the level, then kept again
    d = decide(step("self_use"), rd(soc=27, **calm), o, "peak_shave", "", 0, True, can_shave=True)
    assert d.command == "charge" and d.reason == "peak buffer: charge back to 30 %"
    hour = T0.astimezone(TZ).strftime("%H:00:00")
    assert decide(step("peak_shave"), rd(soc=29, **calm), o, "charge", hour, d.amps, True, can_shave=True).command == "none"
    assert decide(step("self_use"), rd(soc=30, **calm), o, "charge", hour, d.amps, True, can_shave=True).command == "peak_shave"
    # not while the house takes the whole limit
    assert decide(step("self_use"), rd(soc=25, load=4.0), o, "peak_shave", "", 0, True, can_shave=True).command == "none"
    assert decide(step("self_use"), rd(soc=25, load=4.0), o, "charge", hour, 20, True, can_shave=True).command == "peak_shave"
    # without the switch or the setting: as before
    assert decide(step("self_use"), rd(soc=25, **calm), o, "self_use", "", 0, True, can_shave=False).command == "none"
    assert decide(step("self_use"), rd(soc=25, **calm), Options(), "self_use", "", 0, True, can_shave=True).command == "none"
    # a planned charge or export is the plan's
    assert decide(step("charge", 60), rd(soc=25, **calm), o, "self_use", "", 0, True, can_shave=True).reason.startswith("plan")


def test_the_plan_stops_at_the_peak_buffer():
    from pilot.planner import Slot, optimize
    from pilot.simulate import Battery
    b = Battery.from_options(Options(battery_min_soc=20, peak_buffer_pct=10))
    slots = [Slot(T0 + timedelta(hours=i), 1.0, 0.6, 0.0, 30.0, 5.0, None, 5.0 if i == 6 else 0.0) for i in range(10)]
    steps = optimize(b, slots, 40, peak_shave=True)
    assert min(s.soc_end for s in steps[:6]) == 30               # the house stops at the buffer
    assert steps[6].action == "peak_shave" and 20 <= steps[6].soc_end < 30    # the 5 kW quarter comes out of it
    assert all(s.action == "peak_shave" for s in steps[7:])
    plain = optimize(b, slots, 40, peak_shave=False)             # no switch: down to the minimum as before
    assert min(s.soc_end for s in plain) == 20


@pytest.mark.slow
async def test_a_peak_at_the_buffer_stays_at_the_import_limit_and_is_charged_back(tmp_path):
    peak = utc(2026, 1, 14, 20, 0)
    load = lambda t: 5.0 if peak <= t < peak + timedelta(minutes=15) else 0.5     # noqa: E731
    results = {}
    for name, pct in (("buffer", 10), ("none", 0)):
        fake = FakeHA(utc(2026, 1, 14, 16, 0), soc=23, load=load, peak_shaving=True)
        fake.s[CONTROL]["state"] = "on"
        app = App(Options(battery_min_soc=10, peak_buffer_pct=pct), fake, data_dir=str(tmp_path / name),
                  clock=lambda fake=fake: fake.now)
        assert await app.setup()
        lowest = 100.0
        while fake.now < utc(2026, 1, 14, 21, 30):
            await app.step()
            fake.advance(timedelta(seconds=30))
            lowest = min(lowest, fake.soc)
        results[name] = (max(r.kwh["import"] * 4 for r in app.records), lowest, fake.soc, app)
    top, lowest, end, app = results["buffer"]
    assert top <= Options().import_limit_kw + 0.15               # the peak came out of the buffer
    assert 14 < lowest < 20 and end == pytest.approx(20, abs=1.5)    # used, and charged back
    assert any(e["command"].startswith("peak") or "peak" in e["command"] for e in app.status()["inverter"]["log"])
    assert results["none"][0] > 4.0 and results["none"][1] <= 11     # before: empty at the peak


async def test_the_peak_buffer_is_off_without_the_peak_shaving_switch(tmp_path):
    fake = FakeHA(utc(2026, 1, 14, 17, 0), soc=24, peak_shaving=False)
    app = App(Options(peak_buffer_pct=10), fake, data_dir=str(tmp_path / "d"), clock=lambda: fake.now)
    assert await app.setup()
    assert app.o.peak_buffer_pct == 0 and any("peak buffer needs" in e["text"] for e in app.events)


def test_the_peak_buffer_must_leave_room():
    from pilot.config import ConfigError
    with pytest.raises(ConfigError):
        Options(battery_min_soc=50, peak_buffer_pct=45).validate()
