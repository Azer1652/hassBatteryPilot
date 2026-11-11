"""The inverter is never left charging or discharging unattended: lost or frozen readings,
failed commands, crashes, someone else's changes, and the export limit of zero-export setups."""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from fake_ha import CHEAP_NIGHT, TZ, FakeHA, utc

from pilot.app import CMD_PROBLEM, App
from pilot.config import Options

CONTROL = "input_boolean.battery_pilot_control"
TOU = "switch.solis_time_of_use"
IMPORT = "sensor.solis_grid_import_power"
SOC = "sensor.solis_battery_soc"
LOAD = "sensor.solis_household_load_power"


@pytest.fixture
def tmpdata(tmp_path):
    return str(tmp_path / "data")


def winter_forecast(day, kwh):
    return {datetime(day.year, day.month, day.day, h, tzinfo=TZ).astimezone(timezone.utc): kwh / 7 for h in range(9, 16)}


async def run_until(app, fake, end, step_s=10):
    while fake.now < end:
        await app.step()
        fake.advance(timedelta(seconds=step_s))


async def charging_night(tmpdata, control=True):
    """A dull January night: the plan grid-charges from 23:00. Returns (app, fake) once charging."""
    fake = FakeHA(utc(2026, 1, 14, 23, 0), soc=20, forecast=winter_forecast(datetime(2026, 1, 15), 2))
    fake.s[CONTROL]["state"] = "on" if control else "off"
    app = App(Options(**CHEAP_NIGHT), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    while fake.mode() != "charge":
        assert fake.now < utc(2026, 1, 15, 7, 0)
        await app.step()
        fake.advance(timedelta(seconds=10))
    return app, fake


def warnings(app, text):
    return [e for e in app.events if text in e["text"]]


# ---------------------------------------------------------------- lost and frozen readings
async def test_lost_grid_reading_stops_charging_after_a_minute_and_it_resumes(tmpdata):
    app, fake = await charging_night(tmpdata)
    fake.override[IMPORT] = "unavailable"
    t0 = fake.now
    await run_until(app, fake, t0 + timedelta(seconds=30))
    assert fake.mode() == "charge"                      # a short gap (Modbus hiccup) changes nothing
    await run_until(app, fake, t0 + timedelta(seconds=80))
    assert fake.s[TOU]["state"] == "off"                # blind for a minute: back to self-use
    assert len(warnings(app, "No usable measurements")) == 1
    await run_until(app, fake, t0 + timedelta(minutes=5))
    assert len(warnings(app, "No usable measurements")) == 1   # said once per outage
    del fake.override[IMPORT]
    await run_until(app, fake, t0 + timedelta(minutes=8))
    assert fake.mode() == "charge"                      # readings back: the plan goes on


async def test_lost_soc_stops_an_export(tmpdata):
    day = datetime(2026, 6, 14, tzinfo=TZ)
    prices = [{"time": (day + timedelta(hours=h)).isoformat(), "price": 0.30 if h % 24 in (19, 20) else 0.10}
              for h in range(48)]
    fake = FakeHA(utc(2026, 6, 14, 18, 55), soc=90, prices=prices, pv=lambda t: 0.0,
                  forecast=winter_forecast(datetime(2026, 6, 15), 30))
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(contract="dynamic"), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    await run_until(app, fake, utc(2026, 6, 14, 19, 5))
    assert fake.mode() == "discharge"
    fake.override[SOC] = "unavailable"
    await run_until(app, fake, utc(2026, 6, 14, 19, 7))
    assert fake.s[TOU]["state"] == "off"


async def test_when_self_use_also_fails_it_keeps_trying(tmpdata):
    app, fake = await charging_night(tmpdata)
    fake.override[IMPORT] = "unavailable"
    fake.fail = lambda domain, service, data: True       # the Solis integration lost the inverter too
    t0 = fake.now
    await run_until(app, fake, t0 + timedelta(minutes=3))
    assert app.solis.mode == "failed" and fake.s[TOU]["state"] == "on"
    fake.fail = None
    await run_until(app, fake, t0 + timedelta(minutes=3, seconds=20))
    assert fake.s[TOU]["state"] == "off"


async def test_a_lost_house_load_sensor_does_not_stop_the_night_charge(tmpdata):
    app, fake = await charging_night(tmpdata)
    target = app.plan.targets[0]["soc"]
    fake.override[LOAD] = "unavailable"                  # the load sensor is flaky all night
    await run_until(app, fake, utc(2026, 1, 15, 7, 0), step_s=30)
    assert fake.soc >= target - 3
    assert not warnings(app, "No usable measurements")


async def test_a_frozen_soc_stops_charging_until_it_moves_again(tmpdata):
    app, fake = await charging_night(tmpdata)
    fake.override[SOC] = fake.s[SOC]["state"]            # the sensor stops updating, still "available"
    t0 = fake.now
    await run_until(app, fake, t0 + timedelta(minutes=25), step_s=30)
    assert fake.mode() == "charge"                       # a slow battery is not a frozen sensor
    await run_until(app, fake, t0 + timedelta(minutes=35), step_s=30)
    assert fake.s[TOU]["state"] == "off" and warnings(app, "does not change")
    await run_until(app, fake, t0 + timedelta(minutes=45), step_s=30)
    assert fake.s[TOU]["state"] == "off"                 # still the same stale value: stays off
    del fake.override[SOC]
    await run_until(app, fake, t0 + timedelta(minutes=50), step_s=30)
    assert fake.mode() == "charge"


# ---------------------------------------------------------------- helper reads
async def test_one_failed_read_of_the_control_switch_changes_nothing(tmpdata):
    app, fake = await charging_night(tmpdata)
    real = fake.state
    failed = {"n": 0}

    async def flaky(eid):
        if eid == CONTROL and failed["n"] < 2:
            failed["n"] += 1
            raise TimeoutError("HA did not answer")
        return await real(eid)

    fake.state = flaky
    n = len(fake.solis_calls())
    await run_until(app, fake, fake.now + timedelta(seconds=40))
    assert failed["n"] == 2
    assert not [c for c in fake.solis_calls()[n:] if c[0] == "switch"]   # no self-use, no full resend


async def test_a_control_switch_that_stays_unreadable_counts_as_off(tmpdata):
    app, fake = await charging_night(tmpdata)
    fake.override[CONTROL] = "unavailable"
    await run_until(app, fake, fake.now + timedelta(seconds=90))
    assert not app.control_on and fake.s[TOU]["state"] == "off"


# ---------------------------------------------------------------- what Battery Pilot leaves on the inverter
async def test_after_a_failed_command_switching_control_off_returns_to_self_use(tmpdata):
    app, fake = await charging_night(tmpdata)
    fake.fail = lambda domain, service, data: domain == "number"
    await run_until(app, fake, utc(2026, 1, 15, 0, 1))  # the renewal at midnight fails half-way
    assert app.solis.mode == "failed"
    fake.fail = None
    fake.s[CONTROL]["state"] = "off"
    await run_until(app, fake, fake.now + timedelta(seconds=60))
    assert fake.s[TOU]["state"] == "off"


async def test_shutdown_after_a_failed_command_returns_to_self_use(tmpdata):
    app, fake = await charging_night(tmpdata)
    app.solis.mode = "failed"
    await app.shutdown()
    assert fake.s[TOU]["state"] == "off"


async def test_a_crash_while_charging_is_cleaned_up_at_the_next_start(tmpdata):
    """The Solis repeats its time-of-use slot every day: left on, it would charge again tomorrow."""
    app, fake = await charging_night(tmpdata)
    # The app dies without shutdown(); it comes back with control switched off.
    fake.s[CONTROL]["state"] = "off"
    app2 = App(Options(**CHEAP_NIGHT), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app2.setup()
    assert fake.s[TOU]["state"] == "off" and warnings(app2, "had stopped while the inverter")
    fake.now += timedelta(days=1)
    assert fake.mode() == "self_use"


async def test_time_of_use_switched_on_by_something_else_is_switched_off(tmpdata):
    fake = FakeHA(utc(2026, 6, 14, 12, 0), soc=60, pv=lambda t: 3.0, forecast=winter_forecast(datetime(2026, 6, 15), 30))
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    await run_until(app, fake, utc(2026, 6, 14, 12, 2))
    assert app.solis.mode == "self_use"
    fake.s[TOU]["state"] = "on"                          # e.g. the Solis app or an old automation
    await run_until(app, fake, utc(2026, 6, 14, 12, 3))
    assert fake.s[TOU]["state"] == "off"


async def test_with_control_off_a_users_own_time_of_use_is_left_alone(tmpdata):
    fake = FakeHA(utc(2026, 6, 14, 12, 0), soc=60, forecast=winter_forecast(datetime(2026, 6, 15), 30))
    fake.s[TOU]["state"] = "on"
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    await run_until(app, fake, utc(2026, 6, 14, 12, 5))
    assert fake.s[TOU]["state"] == "on" and not fake.solis_calls()


# ---------------------------------------------------------------- failed commands
async def test_a_lasting_command_failure_is_reported_once_in_a_while_and_recovers(tmpdata):
    app, fake = await charging_night(tmpdata)
    fake.override[IMPORT] = "unavailable"                # forces a self-use command every tick
    fake.fail = lambda domain, service, data: True
    t0 = fake.now
    await run_until(app, fake, t0 + timedelta(minutes=30))
    failures = [e for e in app.events if "failed" in e["text"] and "Inverter command" in e["text"]]
    assert 1 <= len(failures) <= 7                       # not one per tick (180)
    assert app.problem and app.problem.startswith(CMD_PROBLEM)
    fake.fail = None
    del fake.override[IMPORT]
    await run_until(app, fake, t0 + timedelta(minutes=33))
    assert app.problem is None and fake.mode() == "charge"


# ---------------------------------------------------------------- the export limit
async def negative_noon(tmpdata, limit_w, restart=False):
    day = datetime(2026, 6, 14, tzinfo=TZ)
    prices = [{"time": (day + timedelta(hours=h)).isoformat(), "price": -0.05 if 12 <= h < 14 else 0.08}
              for h in range(48)]
    fake = FakeHA(utc(2026, 6, 14, 11, 50), soc=60, prices=prices, pv=lambda t: 4.0)
    fake.s[CONTROL]["state"] = "on"
    fake.s["input_boolean.battery_pilot_block_negative_export"]["state"] = "on"
    fake.s["number.solis_export_power_limit"]["state"] = str(limit_w)
    app = App(Options(contract="dynamic"), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    await run_until(app, fake, utc(2026, 6, 14, 12, 5), step_s=30)
    if restart:
        app = App(Options(contract="dynamic"), fake, data_dir=tmpdata, clock=lambda: fake.now)
        assert await app.setup()
    await run_until(app, fake, utc(2026, 6, 14, 14, 5), step_s=30)
    return [c[2]["value"] for c in fake.calls if c[2].get("entity_id") == "number.solis_export_power_limit"], fake, app


async def test_a_zero_export_installation_is_never_given_export(tmpdata):
    writes, fake, _ = await negative_noon(tmpdata, 0)
    assert writes == [] and fake.s["number.solis_export_power_limit"]["state"] == "0"


@pytest.mark.parametrize("restart", [False, True])
async def test_the_export_limit_found_before_a_block_is_restored(tmpdata, restart):
    writes, fake, _ = await negative_noon(tmpdata, 3000, restart)
    assert writes == [0, 3000]                           # not the 5 kW option: what the installation had


@pytest.mark.parametrize("restart", [False, True])
async def test_quarters_with_blocked_export_are_marked(tmpdata, restart):
    """With export at 0 the inverter holds the panels back. Those quarters are marked, so their low
    solar is not learned as shade and not replayed as what the panels could give."""
    _, _, app = await negative_noon(tmpdata, 3000, restart)
    marked = {r.start.astimezone(TZ).strftime("%H:%M") for r in app.records if r.blocked}
    assert marked == {"12:00", "12:15", "12:30", "12:45", "13:00", "13:15", "13:30", "13:45"}
    saved = {r.start.astimezone(TZ).strftime("%H:%M"): r.to_json() for r in app.records}
    assert saved["12:00"]["blocked"] is True and "blocked" not in saved["11:45"]      # kept in the state file


# ---------------------------------------------------------------- a damaged state file
@pytest.mark.parametrize("content", ["null", "[]", "", '{"peaks": {', '{"peaks": {"months": null}}',
                                     '{"shade": {"factors": 1}}', '{"prices": [[1, 2]]}',
                                     '{"records": [{"start": "x"}]}'])
async def test_a_damaged_state_file_does_not_stop_battery_pilot(tmpdata, content):
    os.makedirs(tmpdata, exist_ok=True)
    with open(os.path.join(tmpdata, "state.json"), "w") as f:
        f.write(content)
    fake = FakeHA(utc(2026, 1, 14, 12))
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    assert warnings(app, "could not be read")
    assert os.path.exists(os.path.join(tmpdata, "state.json.bad"))
    with open(os.path.join(tmpdata, "state.json")) as f:
        assert isinstance(json.load(f), dict)            # a good one is written again
