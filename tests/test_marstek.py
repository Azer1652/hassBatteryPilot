"""A Marstek Venus (local API with the cloud as a backup) beside a Solax, through the whole app."""
import logging
from datetime import datetime, timedelta, timezone

import pytest
from fake_ha import FakeHA, utc

from pilot import marstek as M
from pilot.app import App
from pilot.config import ConfigError, Options, from_dict
from pilot.discovery import resolve_all

CONTROL = "input_boolean.battery_pilot_control"
FILL = "input_boolean.battery_pilot_fill_battery_for_car"
MARSTEK = {"battery_system": "marstek"}


@pytest.fixture
def tmpdata(tmp_path):
    return str(tmp_path / "data")


async def run_until(app, fake, end, step_s=30, each=None):
    while fake.now < end:
        await app.step()
        if each:
            each(app, fake)
        fake.advance(timedelta(seconds=step_s))


async def start(tmpdata, fake, control=True, **opts):
    fake.s[CONTROL]["state"] = "on" if control else "off"
    app = App(Options(**{**MARSTEK, **opts}), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup(), app.problem
    return app


def passive(fake):
    return [c for c in fake.calls if c[0] == "marstek_local_api"]


# ---------------------------------------------------------------- pure parts

def test_choose_prefers_a_fresh_local_reading():
    r = M.choose(M.Source(50, 1.0, 20), M.Source(48, 0.9, 30), 300)
    assert (r.soc, r.kw, r.source) == (50, 1.0, "local")


def test_choose_falls_back_to_the_cloud_when_the_local_api_is_silent():
    r = M.choose(M.Source(50, 1.0, 400), M.Source(48, 0.9, 60), 300)
    assert (r.soc, r.source) == (48, "cloud")
    assert "400 s" in r.why
    # a reading whose age is unknown is not used: both integrations keep their last values
    r = M.choose(M.Source(None, None, None), M.Source(48, 0.9, None), 300)
    assert r.source == "none" and "unknown" in r.why
    r = M.choose(M.Source(50, 1.0, None), M.Source(48, 0.9, 60), 300)
    assert r.source == "cloud"


def test_choose_gives_nothing_when_both_are_old():
    r = M.choose(M.Source(50, 1.0, 400), M.Source(48, 0.9, 900), 300)
    assert (r.soc, r.kw, r.source) == (None, None, "none")


def test_report_time_in_epoch_seconds_milliseconds_or_iso():
    now = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    t = now - timedelta(seconds=90)
    assert M.age_s(str(int(t.timestamp())), now) == pytest.approx(90)
    assert M.age_s(str(int(t.timestamp() * 1000)), now) == pytest.approx(90)
    assert M.age_s(t.isoformat(), now) == pytest.approx(90)
    assert M.age_s("unavailable", now) is None and M.age_s("12", now) is None
    # without a zone: Home Assistant's
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Brussels")
    assert M.age_s(t.astimezone(tz).replace(tzinfo=None).isoformat(), now, tz) == pytest.approx(90)


def test_battery_system_is_checked():
    with pytest.raises(ConfigError):
        from_dict({"battery_system": "tesla"})
    assert from_dict({"battery_system": "marstek", "marstek_device_id": " abc "}).marstek_device_id == "abc"


async def test_discovery_finds_the_solax_and_both_marstek_integrations():
    fake = FakeHA(utc(2026, 11, 19, 12), marstek=True)
    states, reg = await fake.states(), fake.registry
    found, missing = resolve_all(Options(**MARSTEK), states, True, reg)
    ids = {k: f.entity_id for k, f in found.items()}
    assert missing == []
    assert ids["grid_import_power"] == "sensor.solax_grid_import"
    assert ids["grid_export_power"] == "sensor.solax_grid_export"
    assert ids["house_load_power"] == "sensor.solax_house_load"
    assert ids["pv_power"] == "sensor.solax_pv_power_total"
    assert ids["export_limit"] == "number.solax_export_control_user_limit"
    assert ids["battery_soc"] == "sensor.marstek_venuse_state_of_charge"
    assert ids["battery_power"] == "sensor.marstek_venuse_power"
    assert ids["marstek_auto_mode_button"] == "button.marstek_venuse_auto_mode"
    assert ids["battery_soc_backup"] == "sensor.venus_state_of_charge"
    assert ids["battery_charge_power_backup"] == "sensor.venus_charge_power"     # not the total charge
    assert ids["battery_report_time_backup"] == "sensor.venus_report_time"
    assert ids["tou_switch"] is None and "not used" in found["tou_switch"].how
    # without the registry the local integration's default names still do
    found, missing = resolve_all(Options(**MARSTEK), states, True, None)
    assert missing == [] and found["battery_soc"].entity_id == "sensor.marstek_venuse_state_of_charge"
    assert found["battery_soc_backup"].entity_id is None


async def test_a_solis_setup_does_not_look_for_marstek_entities():
    fake = FakeHA(utc(2026, 11, 19, 12))
    found, missing = resolve_all(Options(), await fake.states())
    assert missing == [] and found["marstek_auto_mode_button"].entity_id is None


async def test_without_the_device_it_stops_with_a_clear_message(tmpdata):
    fake = FakeHA(utc(2026, 11, 19, 12), marstek=True)
    fake.registry = []
    app = App(Options(**MARSTEK), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert not await app.setup()
    assert "marstek_device_id" in app.problem
    app = App(Options(**MARSTEK, marstek_device_id="dev1"), fake, data_dir=tmpdata + "2", clock=lambda: fake.now)
    assert await app.setup()


# ---------------------------------------------------------------- the whole app

async def test_fill_switch_charges_through_passive_mode_and_renews_it(tmpdata):
    fake = FakeHA(utc(2026, 11, 19, 12), soc=40, marstek=True)
    app = await start(tmpdata, fake)
    fake.s[FILL]["state"] = "on"
    await run_until(app, fake, fake.now + timedelta(minutes=20))
    calls = passive(fake)
    assert calls, fake.calls
    assert all(c[2]["power"] < 0 and c[2]["device_id"] == "dev1" for c in calls)
    assert all(c[2]["duration"] == M.PASSIVE_S for c in calls)
    assert M.PASSIVE_S == 300
    assert len(calls) >= 8                           # sent again every RENEW_S (2 min)
    assert app.solis.mode == "charge" and fake.soc > 41
    assert app.battery_source == "local"
    assert app.status()["inverter"]["source"] == "local"
    # import stays under the limit while it charges
    assert fake.import_kw <= Options().import_limit_kw + 0.1


async def test_house_load_leaves_the_marstek_out(tmpdata):
    fake = FakeHA(utc(2026, 11, 19, 12), soc=40, load=lambda t: 0.6, marstek=True)
    app = await start(tmpdata, fake)
    fake.s[FILL]["state"] = "on"
    await run_until(app, fake, fake.now + timedelta(minutes=5))
    assert app.solis.mode == "charge"
    assert float(fake.s["sensor.solax_house_load"]["state"]) > 1500     # the Solax counts the charge
    assert app.readings.load_kw == pytest.approx(0.6, abs=0.05)


async def test_silent_local_api_reads_the_cloud_and_retries_commands(tmpdata, caplog):
    caplog.set_level(logging.INFO)
    fake = FakeHA(utc(2026, 11, 19, 12), soc=40, marstek=True)
    app = await start(tmpdata, fake)
    fake.s[FILL]["state"] = "on"
    await run_until(app, fake, fake.now + timedelta(minutes=3))
    assert app.solis.mode == "charge"
    fake.local_down = True
    sent = []
    await run_until(app, fake, fake.now + timedelta(minutes=12),
                    each=lambda app, fake: sent.append(fake.now) if len(fake.call_timeouts) > len(sent) else None)
    assert app.battery_source == "cloud"
    assert app.readings is not None and app.readings.soc == round(fake.soc)     # the cloud's live value
    assert "readings from the cloud" in caplog.text
    assert app.solis.mode == "failed"                # the renewal failed
    assert set(fake.call_timeouts) == {M.CALL_TIMEOUT_S}
    assert all((b - a).total_seconds() >= M.RETRY_S for a, b in zip(sent, sent[1:])), sent    # a rest between tries
    assert "did not answer" in caplog.text and "Home Assistant does not answer" not in caplog.text
    assert app.problem and "does not accept commands" in app.problem       # counted as command failures
    n = len(passive(fake))
    fake.local_down = False
    await run_until(app, fake, fake.now + timedelta(minutes=2))
    assert len(passive(fake)) > n and app.solis.mode == "charge"
    assert app.battery_source == "local" and "local API answers again" in caplog.text


async def test_both_links_silent_stops_the_charge_safely(tmpdata):
    fake = FakeHA(utc(2026, 11, 19, 12), soc=40, marstek=True)
    app = await start(tmpdata, fake)
    fake.s[FILL]["state"] = "on"
    await run_until(app, fake, fake.now + timedelta(minutes=3))
    fake.local_down = fake.cloud_down = True
    await run_until(app, fake, fake.now + timedelta(minutes=12))
    assert app.battery_source == "none" and app.readings is None
    assert app.decision.command == "self_use"        # what the blind rule asks (it cannot get through)
    # the passive command lapses by itself: the battery is back in its own mode
    await run_until(app, fake, fake.now + timedelta(minutes=M.PASSIVE_S // 60))
    assert fake.mode() == "self_use"
    fake.local_down = fake.cloud_down = False
    fake.s[FILL]["state"] = "off"
    await run_until(app, fake, fake.now + timedelta(minutes=2))
    assert app.solis.mode == "self_use"
    assert fake.calls[-1][:2] == ("button", "press")


async def test_control_off_presses_auto_mode(tmpdata):
    fake = FakeHA(utc(2026, 11, 19, 12), soc=40, marstek=True)
    app = await start(tmpdata, fake)
    fake.s[FILL]["state"] = "on"
    await run_until(app, fake, fake.now + timedelta(minutes=3))
    fake.s[CONTROL]["state"] = "off"
    await run_until(app, fake, fake.now + timedelta(minutes=1))
    assert app.solis.mode == "self_use" and fake.mode() == "self_use"
    assert ("button", "press", {"entity_id": "button.marstek_venuse_auto_mode"}) in fake.calls


async def test_shadow_mode_never_touches_the_marstek(tmpdata):
    fake = FakeHA(utc(2026, 11, 19, 12), soc=40, marstek=True)
    app = await start(tmpdata, fake, control=False)
    await run_until(app, fake, fake.now + timedelta(hours=2))
    assert not passive(fake) and not [c for c in fake.calls if c[0] == "button"]
    assert app.readings is not None and abs(app.readings.soc - fake.soc) < 1.5     # the battery feeds the house


async def test_winter_night_charge_follows_the_plan(tmpdata):
    """The same winter night as test_app's Solis test: the plan charges the Marstek at night, under
    the import limit, through a local outage in the middle, and hands back to Auto mode by day."""
    from fake_ha import CHEAP_NIGHT, TZ

    def winter(d, kwh):
        return {datetime(d.year, d.month, d.day, h, tzinfo=TZ).astimezone(timezone.utc): kwh / 7 for h in range(9, 16)}

    fc = {**winter(datetime(2026, 1, 14), 3), **winter(datetime(2026, 1, 15), 3)}
    fake = FakeHA(utc(2026, 1, 14, 21, 0), soc=30, forecast=fc, marstek=True)
    app = await start(tmpdata, fake, **CHEAP_NIGHT)
    target = app.plan.targets[0]
    assert target["winter"] and target["soc"] >= 60
    worst = {"import": 0.0}

    def watch(app, fake):
        worst["import"] = max(worst["import"], fake.import_kw)
        fake.local_down = utc(2026, 1, 15, 2, 0) <= fake.now < utc(2026, 1, 15, 2, 20)

    await run_until(app, fake, utc(2026, 1, 15, 7, 30), each=watch)
    assert fake.soc >= target["soc"] - 5, (fake.soc, target)
    assert worst["import"] <= Options().import_limit_kw + 0.35
    assert app.solis.mode == "self_use" and fake.mode() == "self_use"


SHAVE = "input_boolean.battery_pilot_force_peak_shaving"


async def test_p1_meter_is_the_grid_reading_and_its_month_peak_counts(tmpdata):
    fake = FakeHA(utc(2026, 11, 19, 12), soc=40, marstek=True, p1_peak=3100)
    app = await start(tmpdata, fake)
    assert app.ent_ids["grid_power"] == "sensor.p1_meter_power"
    await run_until(app, fake, fake.now + timedelta(minutes=2))
    assert app.peaks.month_peak(fake.now, app.tz) == pytest.approx(3.1)


async def test_peak_shaving_done_by_battery_pilot_keeps_the_grid_at_the_limit(tmpdata):
    load = {"kw": 4.0}
    fake = FakeHA(utc(2026, 11, 19, 18), soc=60, load=lambda t: load["kw"], marstek=True)
    app = await start(tmpdata, fake)
    assert app.can_peak_shave and app.can_solar_hold
    fake.s[SHAVE]["state"] = "on"
    await run_until(app, fake, fake.now + timedelta(minutes=5), step_s=10)
    assert app.solis.mode == "peak_shave"
    assert fake.import_kw == pytest.approx(Options().import_limit_kw, abs=0.2)     # the battery gives the rest
    assert 1400 <= app.solis.power_w <= 1900
    load["kw"] = 1.0                                # under the limit: the battery stops giving
    await run_until(app, fake, fake.now + timedelta(minutes=3), step_s=10)
    assert app.solis.power_w == 0 and fake.import_kw == pytest.approx(1.0, abs=0.05)
    n = len(passive(fake))
    await run_until(app, fake, fake.now + timedelta(minutes=6), step_s=10)
    assert len(passive(fake)) > n                   # renewed before it lapses
    fake.s[SHAVE]["state"] = "off"
    await run_until(app, fake, fake.now + timedelta(minutes=1), step_s=10)
    assert app.solis.mode != "peak_shave"


async def test_peak_shaving_stores_solar_surplus_and_keeps_the_level(tmpdata):
    fake = FakeHA(utc(2026, 6, 19, 12), soc=50, load=lambda t: 0.5, pv=lambda t: 2.0, marstek=True)
    app = await start(tmpdata, fake)
    fake.s[SHAVE]["state"] = "on"
    await run_until(app, fake, fake.now + timedelta(minutes=5), step_s=10)
    assert app.solis.power_w < -1200 and float(fake.s["sensor.solax_grid_export"]["state"]) < 200
    # at the shaving level the battery gives nothing
    fake2 = FakeHA(utc(2026, 11, 19, 18), soc=10, load=lambda t: 4.0, marstek=True)
    app2 = await start(tmpdata + "2", fake2)
    fake2.s[SHAVE]["state"] = "on"
    await run_until(app2, fake2, fake2.now + timedelta(minutes=3), step_s=10)
    assert app2.solis.mode == "peak_shave" and app2.solis.power_w == 0


async def test_full_power_charges_at_the_limit_not_paced(tmpdata):
    from pilot.controller import Readings, decide
    from pilot.planner import Step

    now = utc(2026, 11, 19, 2)
    step = Step(now, 1.0, "charge", 40.0, 45.0, 0, 0, 0, 0, 0, 0, 0)
    r = Readings(now, now, 40.0, 0.3, 0.0, 0.3, 0.3, 51.2)
    paced = decide(step, r, Options(**MARSTEK), "self_use", "", 0, True)
    fast = decide(step, r, Options(**MARSTEK, full_power=True), "self_use", "", 0, True)
    assert paced.command == fast.command == "charge"
    assert paced.amps < fast.amps == 35           # the import limit, not the hour


async def test_without_a_house_load_sensor_the_load_is_the_balance(tmpdata):
    """P1 for the grid, the Solax for solar, the Marstek for the battery: house = grid + solar + battery."""
    fake = FakeHA(utc(2026, 6, 19, 12), soc=40, load=lambda t: 1.2, pv=lambda t: 0.4, marstek=True)
    del fake.s["sensor.solax_house_load"], fake.s["sensor.solax_house_load_alt"]
    real = fake._marstek_sensors

    def sensors(grid, batt, dt):
        fake.s["sensor.solax_house_load"] = {"state": "0", "attributes": {}}
        real(grid, batt, dt)
        del fake.s["sensor.solax_house_load"]

    fake._marstek_sensors = sensors
    app = await start(tmpdata, fake)
    assert "house_load_power" not in app.ent_ids
    fake.s[FILL]["state"] = "on"
    await run_until(app, fake, fake.now + timedelta(minutes=4))
    assert app.solis.mode == "charge"
    assert app.readings.load_kw == pytest.approx(1.2, abs=0.1)
    assert app.readings.pv_kw == pytest.approx(0.4, abs=0.01)


def test_follow_waits_a_minute_but_not_over_the_limit():
    import asyncio

    calls = []

    class HA:
        async def call(self, domain, service, data, timeout=30):
            calls.append(data["power"])

    m = M.Marstek(HA(), {}, device_id="d")
    t = utc(2026, 11, 19, 18)

    async def go():
        await m.peak_shave(t, 2.3, 10)
        calls.clear()
        await m.follow(t + timedelta(seconds=10), 3.0, 60)     # 0.7 kW over: not urgent, too soon
        assert calls == []
        await m.follow(t + timedelta(seconds=60), 3.0, 60)
        assert calls == [700]
        await m.follow(t + timedelta(seconds=80), 3.5, 60)     # 1.2 kW over: urgent, but 30 s apart
        assert calls == [700]
        await m.follow(t + timedelta(seconds=90), 3.5, 60)
        assert calls == [700, 1900]
        await m.follow(t + timedelta(seconds=120), 2.4, 60)    # under 200 W of change: not sent
        assert len(calls) == 2
        await m.follow(t + timedelta(seconds=90 + M.RENEW_S), 2.3, 60)   # renewed before it lapses
        assert len(calls) == 3

    asyncio.run(go())


def test_commands_are_clamped_to_the_battery_maximum():
    import asyncio

    calls = []

    class HA:
        async def call(self, domain, service, data, timeout=30):
            calls.append(data["power"])

    m = M.Marstek(HA(), {}, device_id="d", max_charge_w=2500, max_discharge_w=800)
    asyncio.run(m.charge(utc(2026, 11, 19, 2), 90))
    asyncio.run(m.discharge(utc(2026, 11, 19, 2), 90))
    assert calls == [-2500, 800]


async def test_peak_shaving_with_a_lagging_battery_stays_near_the_limit(tmpdata):
    fake = FakeHA(utc(2026, 11, 19, 18), soc=60, load=lambda t: 4.0, marstek=True)
    fake.marstek_lag_s = 10
    app = await start(tmpdata, fake)
    fake.s[SHAVE]["state"] = "on"
    sends = []
    await run_until(app, fake, fake.now + timedelta(minutes=10), step_s=10,
                    each=lambda app, fake: sends.append(len(passive(fake))))
    assert fake.import_kw == pytest.approx(Options().import_limit_kw, abs=0.25)
    # steady state: no more than one command a minute (renewals included)
    assert sends[-1] - sends[-31] <= 5


def test_p1_month_peak_is_ignored_in_the_first_quarter_of_the_month():
    from zoneinfo import ZoneInfo

    from pilot.meter import PeakTracker

    tz = ZoneInfo("Europe/Brussels")
    p = PeakTracker()
    p.observe(datetime(2026, 12, 1, 0, 0, 10, tzinfo=tz), tz, 4.2)       # still November's
    assert p.month_peak(datetime(2026, 12, 1, 1, tzinfo=tz), tz) == 0.0
    p.observe(datetime(2026, 12, 1, 0, 20, tzinfo=tz), tz, 1.1)
    assert p.month_peak(datetime(2026, 12, 1, 1, tzinfo=tz), tz) == pytest.approx(1.1)


async def test_unparsable_cloud_report_time_means_no_cloud_reading(tmpdata):
    fake = FakeHA(utc(2026, 11, 19, 12), soc=40, marstek=True)
    app = await start(tmpdata, fake)
    await run_until(app, fake, fake.now + timedelta(minutes=1))
    fake.local_down = True
    real = fake._marstek_sensors

    def sensors(grid, batt, dt):
        real(grid, batt, dt)
        fake.s["sensor.venus_report_time"]["state"] = "yesterday-ish"

    fake._marstek_sensors = sensors
    await run_until(app, fake, fake.now + timedelta(minutes=7))
    assert app.battery_source == "none" and app.readings is None


async def test_marstek_settings_left_at_the_solis_defaults_are_warned_about(tmpdata, caplog):
    fake = FakeHA(utc(2026, 11, 19, 12), soc=40, marstek=True)
    await start(tmpdata, fake)
    assert "look like a Solis" in caplog.text
    caplog.clear()
    await start(tmpdata + "2", fake, battery_capacity_kwh=5.12, max_charge_current_a=48, max_discharge_current_a=48)
    assert "look like a Solis" not in caplog.text


async def test_the_last_message_sensor_is_required(tmpdata):
    fake = FakeHA(utc(2026, 11, 19, 12), marstek=True)
    found, missing = resolve_all(Options(**MARSTEK), {k: v for k, v in (await fake.states()).items()
                                                      if "last_message" not in k}, True, fake.registry)
    assert "marstek_last_message" in missing


async def test_auto_mode_through_the_operating_mode_select(tmpdata):
    """Without the Auto mode button, the operating-mode select picks Auto."""
    from fake_ha import st

    fake = FakeHA(utc(2026, 11, 19, 12), soc=40, marstek=True)
    del fake.s["button.marstek_venuse_auto_mode"]
    fake.registry = [e for e in fake.registry if e["entity_id"] != "button.marstek_venuse_auto_mode"]
    fake.s["select.marstek_venuse_operating_mode"] = st("Manual", options=["Auto", "AI", "Manual", "Passive"])
    fake.registry.append({"entity_id": "select.marstek_venuse_operating_mode", "platform": "marstek_local_api",
                          "unique_id": "aabbcc_operating_mode", "device_id": "dev1", "disabled_by": None})
    fake.registry.append({"entity_id": "sensor.marstek_venuse_operating_mode", "platform": "marstek_local_api",
                          "unique_id": "aabbcc_operating_mode", "device_id": "dev1", "disabled_by": None})
    fake.s["sensor.marstek_venuse_operating_mode"] = st("Manual")
    app = await start(tmpdata, fake)
    assert app.ent_ids["marstek_mode_select"] == "select.marstek_venuse_operating_mode"
    fake.s[FILL]["state"] = "on"
    await run_until(app, fake, fake.now + timedelta(minutes=3))
    assert app.solis.mode == "charge"
    fake.s[CONTROL]["state"] = "off"
    await run_until(app, fake, fake.now + timedelta(minutes=1))
    assert ("select", "select_option", {"entity_id": "select.marstek_venuse_operating_mode", "option": "Auto"}) \
        in fake.calls
    assert fake.mode() == "self_use" and fake.s["select.marstek_venuse_operating_mode"]["state"] == "Auto"


def test_auto_option_spellings():
    assert M.auto_option(["AI", "Auto", "Manual"]) == "Auto"
    assert M.auto_option(["ai", "auto_mode", "manual"]) == "auto_mode"
    assert M.auto_option(None) == "Auto"


async def test_without_the_passive_action_it_says_which_integration(tmpdata):
    fake = FakeHA(utc(2026, 11, 19, 12), marstek=True)
    real = fake.services

    async def services():
        return [d for d in await real() if d["domain"] != "marstek_local_api"]

    fake.services = services
    app = App(Options(**MARSTEK), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert not await app.setup()
    assert "set_passive_mode" in app.problem
