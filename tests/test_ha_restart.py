"""A restart of Home Assistant: the Supervisor proxy answers 502 for a while. No command can reach
the inverter then, so it is left alone, and Battery Pilot picks up again when Home Assistant is back."""
import logging
from datetime import date, datetime, timedelta, timezone

import pytest
from fake_ha import CHEAP_NIGHT, TZ, FakeHA, utc

from pilot.app import BLIND_S, HA_AWAY_PROBLEM, HA_BACK_GRACE_S, App
from pilot.config import Options
from pilot.ha import HAUnreachable

CONTROL = "input_boolean.battery_pilot_control"
FORCE_SHAVE = "input_boolean.battery_pilot_force_peak_shaving"
SOC = "sensor.solis_battery_soc"


@pytest.fixture
def tmpdata(tmp_path):
    return str(tmp_path / "data")


async def run_until(app, fake, end, step_s=10):
    while fake.now < end:
        try:
            await app.step()
        except Exception as e:  # noqa: BLE001 - what App.run does with an error out of a round
            app._error(e)
        fake.advance(timedelta(seconds=step_s))


def texts(app, text):
    return [e["text"] for e in app.events if text in e["text"]]


async def shaving(tmpdata):
    """A January afternoon, peak shaving (forced by the switch)."""
    fake = FakeHA(utc(2026, 1, 14, 14, 0), soc=60, peak_shaving=True)
    fake.s[CONTROL]["state"] = "on"
    fake.s[FORCE_SHAVE]["state"] = "on"
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    await run_until(app, fake, fake.now + timedelta(minutes=1))
    assert fake.mode() == "peak_shave"
    return app, fake


def winter_forecast(day, kwh):
    return {datetime(day.year, day.month, day.day, h, tzinfo=TZ).astimezone(timezone.utc): kwh / 7
            for h in range(9, 16)}


async def charging_night(tmpdata):
    fake = FakeHA(utc(2026, 1, 14, 23, 0), soc=20, forecast=winter_forecast(date(2026, 1, 15), 2))
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(**CHEAP_NIGHT), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    while fake.mode() != "charge":
        assert fake.now < utc(2026, 1, 15, 7, 0)
        await app.step()
        fake.advance(timedelta(seconds=10))
    return app, fake


async def test_a_restart_of_home_assistant_leaves_the_inverter_alone(tmpdata, caplog):
    app, fake = await shaving(tmpdata)
    calls = len(fake.calls)
    caplog.set_level(logging.INFO)
    caplog.clear()
    fake.away = True
    t0 = fake.now
    await run_until(app, fake, t0 + timedelta(minutes=3))
    assert len(fake.calls) == calls and fake.mode() == "peak_shave"
    assert texts(app, "Home Assistant does not answer (restarting?): the inverter keeps its current "
                      "setting (peak shaving)") and len(texts(app, "does not answer (restarting?)")) == 1
    assert app.problem == HA_AWAY_PROBLEM
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], caplog.text

    fake.away = False
    back = fake.now
    await run_until(app, fake, back + timedelta(minutes=1))
    assert len(texts(app, "Home Assistant is back after")) == 1
    assert app.problem is None
    sent = [c for c in fake.calls[calls:] if c[1] == "turn_on" and c[2].get("entity_id") ==
            "switch.solis_peak_shaving_mode"]
    assert len(sent) == 1                              # peak shaving sent again once
    assert fake.mode() == "peak_shave" and app.solis.mode == "peak_shave"
    assert app._last["refresh"] is not None and app._last["refresh"] >= back   # prices and forecast read again
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], caplog.text


async def test_a_charge_across_a_restart_is_not_stopped_and_is_sent_again(tmpdata, caplog):
    app, fake = await charging_night(tmpdata)
    caplog.set_level(logging.INFO)
    caplog.clear()
    fake.away = True
    await run_until(app, fake, fake.now + timedelta(minutes=2))
    assert fake.mode() == "charge"
    assert not texts(app, "No usable measurements")
    fake.away = False
    calls = len(fake.calls)
    await run_until(app, fake, fake.now + timedelta(seconds=20))
    assert fake.mode() == "charge" and app.solis.mode == "charge"
    assert len(fake.calls) > calls                     # the charge was sent again
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], caplog.text


async def test_entities_still_loading_after_the_restart_get_the_blind_minute_afresh(tmpdata):
    app, fake = await charging_night(tmpdata)
    fake.away = True
    await run_until(app, fake, fake.now + timedelta(minutes=2))
    fake.away = False
    fake.override[SOC] = "unavailable"                 # the Solis Modbus integration is still loading
    back = fake.now
    await run_until(app, fake, back + timedelta(seconds=BLIND_S - 10))
    assert not texts(app, "No usable measurements") and fake.mode() == "charge"
    await run_until(app, fake, back + timedelta(seconds=BLIND_S + 30))
    assert len(texts(app, "No usable measurements")) == 1      # it lasts: stopped as before
    del fake.override[SOC]


async def test_a_command_cut_off_by_the_restart_is_not_an_inverter_error(tmpdata, caplog):
    app, fake = await shaving(tmpdata)
    caplog.set_level(logging.INFO)
    caplog.clear()

    def cut_off(domain, service, data):                # the reads still answer, the command does not
        raise HAUnreachable(f"POST /services/{domain}/{service}: HTTP 502 502: Bad Gateway")

    fake.fail = cut_off
    fake.s[FORCE_SHAVE]["state"] = "off"
    fake.s[CONTROL]["state"] = "off"                   # control off: a self-use command is due
    await run_until(app, fake, fake.now + timedelta(minutes=2))
    assert len(texts(app, "not sent: Home Assistant does not answer")) == 1
    assert app._cmd_failures == 0 and app.problem is None
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], caplog.text


async def test_a_forecast_still_missing_after_the_restart_is_reported_once(tmpdata):
    fake = FakeHA(utc(2026, 1, 14, 10, 0), forecast=winter_forecast(date(2026, 1, 14), 3))
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    fake.away = True
    await run_until(app, fake, fake.now + timedelta(minutes=1))
    fake.forecast = {}                                 # the forecast integration is not loaded yet
    fake.away = False
    await run_until(app, fake, fake.now + timedelta(seconds=HA_BACK_GRACE_S - 30))
    assert not texts(app, "solar forecast is unavailable")
    await run_until(app, fake, fake.now + timedelta(minutes=6))
    assert len(texts(app, "solar forecast is unavailable")) == 1
