"""The whole app against a simulated Home Assistant, house, battery and Solis."""
import os
from datetime import datetime, timedelta, timezone

import pytest

from fake_ha import TZ, FakeHA, utc
from pilot.app import App
from pilot.config import Options
from pilot.meter import QuarterRecord
from pilot.migrate import MARK

CONTROL = "input_boolean.battery_pilot_control"


def winter_forecast(day_local, kwh):
    """Hourly forecast for a day: kwh spread over 09-16 local."""
    out = {}
    for h in range(9, 16):
        out[datetime(day_local.year, day_local.month, day_local.day, h, tzinfo=TZ).astimezone(timezone.utc)] = kwh / 7
    return out


async def run_until(app, fake, end, step_s=30, each=None):
    while fake.now < end:
        await app.step()
        if each:
            each(app, fake)
        fake.advance(timedelta(seconds=step_s))


@pytest.fixture
def tmpdata(tmp_path):
    return str(tmp_path / "data")


async def test_winter_night_charges_to_target_under_the_import_limit(tmpdata):
    start = utc(2026, 1, 14, 21, 0)
    spike = (utc(2026, 1, 15, 1, 32), utc(2026, 1, 15, 1, 42))
    load = lambda t: 3.0 if spike[0] <= t < spike[1] else 0.5
    fc = {**winter_forecast(datetime(2026, 1, 14), 3), **winter_forecast(datetime(2026, 1, 15), 3)}
    fake = FakeHA(start, soc=30, load=load, forecast=fc)
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    target = app.plan.targets[0]
    assert target["winter"] and target["soc"] >= 60

    seen = {"max_import_ok": 0.0, "charged_in_spike": False, "max_soc": 0.0}

    def watch(app, fake):
        in_spike = spike[0] <= fake.now < spike[1] + timedelta(minutes=2)
        if not in_spike:
            seen["max_import_ok"] = max(seen["max_import_ok"], fake.import_kw)
        elif fake.now >= spike[0] + timedelta(minutes=1) and fake.mode() == "charge" and fake.load_fn(fake.now) > 2:
            seen["charged_in_spike"] = True
        seen["max_soc"] = max(seen["max_soc"], fake.soc)

    await run_until(app, fake, utc(2026, 1, 15, 7, 0), each=watch)
    assert fake.soc >= target["soc"] - 3, (fake.soc, target)
    assert seen["max_soc"] <= target["soc"] + 4          # no overcharge (old bug 1)
    assert not seen["charged_in_spike"]                  # the battery covers a cooking spike
    assert seen["max_import_ok"] <= Options().import_limit_kw + 0.35
    await run_until(app, fake, utc(2026, 1, 15, 7, 30))
    assert fake.s["switch.solis_time_of_use"]["state"] == "off"   # back to self-use by day
    assert "sensor.battery_pilot_plan" in fake.published
    assert fake.published["sensor.battery_pilot_plan"]["attributes"]["mode"] == "active"
    assert os.path.exists(os.path.join(tmpdata, "state.json"))


async def test_shadow_mode_never_touches_the_inverter(tmpdata):
    start = utc(2026, 1, 14, 21, 0)
    fc = winter_forecast(datetime(2026, 1, 15), 3)
    fake = FakeHA(start, soc=30, forecast=fc)
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    await run_until(app, fake, utc(2026, 1, 15, 2, 0), step_s=60)
    assert fake.solis_calls() == []
    assert app.plan is not None and any(s.action == "charge" for s in app.plan.steps)
    assert fake.published["sensor.battery_pilot_plan"]["attributes"]["mode"] == "shadow"


async def test_switching_control_off_returns_to_self_use(tmpdata):
    start = utc(2026, 1, 14, 22, 30)
    fake = FakeHA(start, soc=20, forecast=winter_forecast(datetime(2026, 1, 15), 2))
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    await run_until(app, fake, utc(2026, 1, 15, 3, 30), step_s=60)
    assert fake.mode() == "charge"
    fake.s[CONTROL]["state"] = "off"
    await run_until(app, fake, utc(2026, 1, 15, 3, 32), step_s=30)
    assert fake.s["switch.solis_time_of_use"]["state"] == "off"


async def test_missing_required_entity_stops_with_a_clear_message(tmpdata):
    fake = FakeHA(utc(2026, 1, 14, 12))
    del fake.s["sensor.solis_battery_soc"]
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert not await app.setup()
    assert "battery_soc" in app.problem


async def test_helpers_are_created_when_missing(tmpdata):
    fake = FakeHA(utc(2026, 1, 14, 12), with_helpers=False)
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    assert set(fake.helpers_created) == {CONTROL, "input_boolean.battery_pilot_block_negative_export"}


async def test_takes_over_from_battery_playbook(tmp_path, tmpdata):
    cfg = tmp_path / "config"
    (cfg / "packages").mkdir(parents=True)
    (cfg / "configuration.yaml").write_text("homeassistant:\n  packages: !include_dir_named packages\n")
    old = cfg / "packages" / "battery_playbook_peak_shaving.yaml"
    old.write_text(MARK + " 1.0.0.\nscript: {}\n")
    mine = cfg / "packages" / "my_lights.yaml"
    mine.write_text("light: []\n")
    fake = FakeHA(utc(2026, 11, 9, 12))
    fake.s["automation.peak_shaving_follow_house_load"] = {"state": "on", "attributes": {"id": "ps_follow_load"}}
    fake.s["automation.my_own"] = {"state": "on", "attributes": {"id": "my_own"}}
    fake.s["sensor.cw_shadow_bill"] = {"state": "2026-11", "attributes": {
        "hist": [{"month": "2026-10", "imp_day": 100, "imp_night": 150, "exp": 80, "belpex": 95,
                  "dyn_imp": 60.5, "dyn_exp": 4.2}],
        "cur": {"month": "2026-11", "imp_day": 20, "imp_night": 30, "exp": 5, "p_sum": 900, "n": 10,
                "dyn_imp": 12, "dyn_exp": 0.3}}}
    fake.s["sensor.ps_daily_consumption_avg"] = {"state": "12.0", "attributes": {}}
    app = App(Options(), fake, data_dir=tmpdata, config_dir=str(cfg), clock=lambda: fake.now)
    assert await app.setup()
    assert fake.s["automation.peak_shaving_follow_house_load"]["state"] == "off"
    assert fake.s["automation.my_own"]["state"] == "on"
    assert not old.exists() and mine.exists()
    assert os.path.exists(os.path.join(tmpdata, "playbook_backup", old.name))
    oct_ = app.comparison.report("2026-10", Options())
    assert oct_["bought_kwh"] == 250 and oct_["market_avg_eur_mwh"] == 95
    assert app.load.default_kw == pytest.approx(0.5)
    # second start: nothing to do any more
    app2 = App(Options(), fake, data_dir=tmpdata, config_dir=str(cfg), clock=lambda: fake.now)
    assert await app2.setup()
    assert app2.migrated


async def test_failed_config_check_keeps_the_old_files(tmp_path, tmpdata):
    cfg = tmp_path / "config"
    (cfg / "packages").mkdir(parents=True)
    old = cfg / "packages" / "battery_playbook_contract_watch.yaml"
    old.write_text(MARK + " 1.0.0.\n")
    fake = FakeHA(utc(2026, 11, 9, 12))
    fake.check_ok = False
    app = App(Options(), fake, data_dir=tmpdata, config_dir=str(cfg), clock=lambda: fake.now)
    assert await app.setup()
    assert old.exists()


async def test_nightly_learning_and_contract_replay(tmpdata):
    day = datetime(2026, 3, 10, tzinfo=TZ)
    start = day.astimezone(timezone.utc)
    prices = []
    for h in range(48):
        t = day + timedelta(hours=h)
        p = 0.04 if h % 24 in (2, 3, 4, 13, 14) else (0.25 if h % 24 in (18, 19, 20) else 0.11)
        prices.append({"time": t.isoformat(), "price": p})
    fc = winter_forecast(datetime(2026, 3, 10), 12)
    fake = FakeHA(start - timedelta(minutes=5), soc=50, forecast=fc, prices=prices)
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    for q in range(96):
        t = start + timedelta(minutes=15 * q)
        hour = t.astimezone(TZ).hour
        pv = 0.7 * (12 / 7) / 4 if 9 <= hour < 16 else 0.0      # 70 % of the forecast: shade
        load = 0.6 / 4 if not 17 <= hour < 21 else 1.5 / 4
        app.records.append(QuarterRecord(t, {"import": max(load - pv, 0), "export": max(pv - load, 0),
                                             "load": load, "pv": pv},
                                         {"import": 1, "export": 1, "load": 1, "pv": 1}, 50))
    for h, v in fc.items():
        app.forecast_archive[h.isoformat()] = v
    fake.now = start + timedelta(days=1, minutes=30)
    await app.nightly()
    m = app.comparison.report("2026-03", Options())
    assert m["replay_days"] == 1
    ws = m["with_strategy"]
    assert ws["fixed"]["eur"] > 0 and ws["dynamic"]["eur"] > 0
    assert ws["dynamic"]["battery_in_kwh"] > 0
    assert app.shade.factor(3, 12) < 0.95                    # learned the shade
    assert app.load.learned()
    s = app.comparison.summary(Options(), fake.now, TZ)
    assert "with_strategy" in s
    await app.publish()
    assert "sensor.battery_pilot_contract_comparison" in fake.published


async def test_renewal_reminder_and_monthly_verdict(tmpdata):
    o = Options(fixed_contract_end="2026-12-23")
    fake = FakeHA(utc(2026, 11, 11, 9, 30))
    app = App(o, fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    await app.daily_alerts()
    titles = [c[2].get("title") for c in fake.calls if c[0] in ("notify", "persistent_notification")]
    assert "Fixed price ends in 42 days" in titles
    n = len(fake.calls)
    await app.daily_alerts()
    assert len(fake.calls) == n                                  # not twice
    app.comparison.month("2026-11")["imp_day"] = 100
    fake.now = utc(2026, 12, 1, 9, 5)
    await app.daily_alerts()
    titles = [c[2].get("title") for c in fake.calls]
    assert "Contract check 2026-11" in titles
    assert ("notify", "mobile_app_phone") in [(c[0], c[1]) for c in fake.calls]


async def test_state_survives_a_restart(tmpdata):
    fake = FakeHA(utc(2026, 1, 14, 12))
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    app.shade.learn(1, 13, 0.3, 1.0)
    app.peaks.months["2026-01"] = 3.3
    app.save_state()
    app2 = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app2.setup()
    assert app2.shade.factors[0][13] == pytest.approx(0.3)
    assert app2.peaks.months["2026-01"] == 3.3


async def test_dashboard_status_is_complete(tmpdata):
    fake = FakeHA(utc(2026, 1, 14, 12))
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    await app.step()
    s = app.status()
    for k in ("plan", "prices", "solar", "load", "peaks", "comparison", "breakeven", "readings", "inverter"):
        assert k in s
    assert s["breakeven"] == {"night": 131, "day": 162}
