"""The whole app against a simulated Home Assistant, house, battery and Solis."""
import logging
import os
from datetime import date, datetime, timedelta, timezone

import pytest
from fake_ha import CHEAP_NIGHT, TZ, FakeHA, utc

from pilot import history as H
from pilot.app import App
from pilot.compare import Comparison
from pilot.config import Options
from pilot.meter import QuarterRecord
from pilot.migrate import MARK
from pilot.web import make_web

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


@pytest.mark.slow
async def test_winter_night_charges_to_target_under_the_import_limit(tmpdata):
    start = utc(2026, 1, 14, 21, 0)
    spike = (utc(2026, 1, 15, 1, 32), utc(2026, 1, 15, 1, 42))
    load = lambda t: 3.0 if spike[0] <= t < spike[1] else 0.5
    fc = {**winter_forecast(datetime(2026, 1, 14), 3), **winter_forecast(datetime(2026, 1, 15), 3)}
    fake = FakeHA(start, soc=30, load=load, forecast=fc)
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(**CHEAP_NIGHT), fake, data_dir=tmpdata, clock=lambda: fake.now)
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
    app = App(Options(**CHEAP_NIGHT), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    await run_until(app, fake, utc(2026, 1, 15, 2, 0), step_s=60)
    assert fake.solis_calls() == []
    assert app.plan is not None and any(s.action == "charge" for s in app.plan.steps)
    assert fake.published["sensor.battery_pilot_plan"]["attributes"]["mode"] == "shadow"


async def test_switching_control_off_returns_to_self_use(tmpdata):
    start = utc(2026, 1, 14, 22, 30)
    fake = FakeHA(start, soc=20, forecast=winter_forecast(datetime(2026, 1, 15), 2))
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(**CHEAP_NIGHT), fake, data_dir=tmpdata, clock=lambda: fake.now)
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
    assert set(fake.helpers_created) == {CONTROL, "input_boolean.battery_pilot_block_negative_export",
                                         "input_boolean.battery_pilot_fill_battery_for_car",
                                         "input_boolean.battery_pilot_force_peak_shaving"}


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
    o = Options()
    oct_ = app.comparison.report("2026-10", o)
    assert oct_["bought_kwh"] == 250 and oct_["market_avg_eur_mwh"] == 95
    # 100 kWh by day, 150 at night, 80 sold at the month's 95 EUR/MWh, the yearly fee and the billed minimum peak
    fixed = (100 * 29.92 + 150 * 26.55 - 80 * (0.0798 * 95 - 2.72 - 0.19)) / 100 + 65 / 12 + 2.5 * 52.37 / 12
    assert oct_["same_usage"]["fixed_eur"] == pytest.approx(fixed, abs=0.01)
    assert oct_["same_usage"]["dynamic_eur"] == pytest.approx(60.5 - 4.2 + 15 / 12 + 2.5 * 52.37 / 12, abs=0.01)
    assert app.load.default_kw == pytest.approx(0.5)
    # second start: nothing to do any more
    app2 = App(Options(), fake, data_dir=tmpdata, config_dir=str(cfg), clock=lambda: fake.now)
    assert await app2.setup()
    assert app2.migrated
    assert app2.comparison.report("2026-10", o)["same_usage"] == oct_["same_usage"]
    # a state saved by 2.4.0 has no separate figures for the priced quarters: the taken-over month keeps its bill
    old_state = {k: v for k, v in app2.comparison.months["2026-10"].items() if not k.startswith("p_")}
    assert Comparison.from_json({"months": {"2026-10": old_state}}).report("2026-10", o)["same_usage"] == oct_["same_usage"]


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


@pytest.mark.parametrize("block", [False, True])
async def test_nightly_learning_and_contract_replay(tmpdata, block):
    day = datetime(2026, 3, 10, tzinfo=TZ)
    start = day.astimezone(timezone.utc)
    prices = []
    for h in range(48):
        t = day + timedelta(hours=h)
        p = 0.04 if h % 24 in (2, 3, 4, 13, 14) else (0.25 if h % 24 in (18, 19, 20) else 0.11)
        prices.append({"time": t.isoformat(), "price": p})
    fc = winter_forecast(datetime(2026, 3, 10), 12)
    fake = FakeHA(start - timedelta(minutes=5), soc=50, forecast=fc, prices=prices)
    fake.s["input_boolean.battery_pilot_block_negative_export"]["state"] = "on" if block else "off"
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    await app.tick()
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
    replayed = [e["text"] for e in app.events if e["text"].startswith("Replayed")]
    assert len(replayed) == 1 and ("export blocked at negative prices" in replayed[0]) is block
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
    assert s["breakeven"] == {"night": 137, "day": 168}
    # the shade tooltips tell a learned cell from one still at the default factor
    app.shade.learn(1, 13, 0.3, 1.0)
    kwh = app.status()["solar"]["shade_kwh"]
    assert len(kwh) == 12 and all(len(row) == 24 for row in kwh)
    assert kwh[0][13] == 1.0 and kwh[0][14] == 0.0
    # the House tab: the learned peaks per weekday and hour, None where nothing is learned yet
    app.load.learn_peak(2, 18, 3.4)
    load = app.status()["load"]
    assert len(load["peak"]) == 7 and all(len(row) == 24 for row in load["peak"])
    assert load["peak"][2][18] == 3.4 and load["peak"][2][19] is None and len(load["kw"]) == 7


async def test_zero_export_setting_is_left_alone(tmpdata):
    fake = FakeHA(utc(2026, 6, 14, 12))
    fake.s["number.solis_export_power_limit"]["state"] = "0"       # a zero-export installation
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    fake.s[CONTROL]["state"] = "on"
    await run_until(app, fake, utc(2026, 6, 14, 12, 10))
    assert not [c for c in fake.calls if c[2].get("entity_id") == "number.solis_export_power_limit"]


async def test_export_blocked_at_negative_prices_and_restored(tmpdata):
    day = datetime(2026, 6, 14, tzinfo=TZ)
    prices = [{"time": (day + timedelta(hours=h)).isoformat(), "price": -0.05 if 12 <= h < 14 else 0.08}
              for h in range(48)]
    fake = FakeHA(utc(2026, 6, 14, 11, 50), soc=60, prices=prices, pv=lambda t: 4.0)
    fake.s[CONTROL]["state"] = "on"
    fake.s["input_boolean.battery_pilot_block_negative_export"]["state"] = "on"
    app = App(Options(contract="dynamic"), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    await run_until(app, fake, utc(2026, 6, 14, 12, 5))
    assert fake.s["number.solis_export_power_limit"]["state"] == "0"
    app2 = App(Options(contract="dynamic"), fake, data_dir=tmpdata, clock=lambda: fake.now)   # survives a restart
    assert await app2.setup()
    await run_until(app2, fake, utc(2026, 6, 14, 14, 5))
    assert fake.s["number.solis_export_power_limit"]["state"] == "5000"


@pytest.mark.parametrize("has_limit", [True, False])
async def test_plan_counts_on_blocking_only_when_the_inverter_can_block(tmpdata, has_limit):
    fake = FakeHA(utc(2026, 6, 14, 12))
    if not has_limit:
        del fake.s["number.solis_export_power_limit"]
    fake.s["input_boolean.battery_pilot_block_negative_export"]["state"] = "on"
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    await app.tick()
    assert app.curtail_on
    assert app.blocks_negative_export is has_limit      # without an export limit nothing gets blocked


async def test_stopping_the_app_while_charging_returns_to_self_use(tmpdata):
    fake = FakeHA(utc(2026, 1, 14, 23, 0), soc=20, forecast=winter_forecast(datetime(2026, 1, 15), 2))
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(**CHEAP_NIGHT), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    while fake.mode() != "charge" and fake.now < utc(2026, 1, 15, 7, 0):
        await app.step()
        fake.advance(timedelta(seconds=60))
    assert fake.mode() == "charge"
    await app.shutdown()
    assert fake.s["switch.solis_time_of_use"]["state"] == "off"


async def test_unreachable_home_assistant_is_explained_once(tmpdata, caplog):
    fake = FakeHA(utc(2026, 1, 14, 12, 0))
    fake.ping_error = "GET /: HTTP 401 401: Unauthorized"
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    with caplog.at_level(logging.WARNING):
        assert not await app.reach_ha()
        assert not await app.reach_ha()
    assert "HTTP 401" in app.problem and "Supervisor token" in app.problem
    assert sum("Waiting for Home Assistant" in r.getMessage() for r in caplog.records) == 1
    fake.ping_error = None
    assert await app.reach_ha()
    assert await app.setup() and app.problem is None


@pytest.mark.slow
async def test_history_planned_vs_actual(tmpdata):
    """A day is kept with the day-ahead plan, the plan of every hour and what really happened."""
    fc = {**winter_forecast(datetime(2026, 3, 10), 12), **winter_forecast(datetime(2026, 3, 11), 12)}
    pv = lambda t: fc.get(t.replace(minute=0, second=0, microsecond=0), 0.0) * 0.6   # the trees take 40 %
    fake = FakeHA(utc(2026, 3, 9, 23, 30), soc=40, pv=pv, forecast=fc)
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    for h in range(9, 16):
        for _ in range(10):
            app.shade.learn(3, h, 0.8, 1.0)                  # what was learned before: 80 % at these hours
    before = app.shade.factor(3, 12)
    await run_until(app, fake, utc(2026, 3, 11, 0, 30), step_s=120)

    assert os.path.exists(os.path.join(tmpdata, "history", "2026-03-10.json"))
    doc = app.history_day(date(2026, 3, 10))
    da = doc["day_ahead"]
    assert 20 <= da["created"] <= 25                          # made right after the nightly learning
    noon = da["hours"][12]
    assert noon[2] == pytest.approx(noon[1] * before, abs=0.01)
    assert len(doc["hourly"]) == 24 and doc["hourly"][0]["created"] < 10
    assert sum(v is not None for v in doc["measured"]["soc"]) >= 96
    assert set(doc["measured"]["mode"]) <= {"charge", "self_use", "discharge"}
    s = doc["summary"]["day_ahead"]
    assert s["solar_forecast"] > s["solar_after_shade"] > s["solar_measured"] > 0
    assert doc["summary"]["hourly"]["soc_mean_abs"] < s["soc_mean_abs"]   # each hour starts from the real SOC

    # Today's day-ahead plan already uses what was learned from yesterday's sun.
    today = app.history_day(date(2026, 3, 11))
    after = app.shade.factor(3, 12)
    assert after < before - 0.02
    noon = today["day_ahead"]["hours"][12]
    assert noon[2] == pytest.approx(noon[1] * after, abs=0.01)
    assert today["live"]["now"] >= 30 and today["live"]["plan"]

    app.save_state()
    app2 = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app2.setup()
    assert app2.day_logs["2026-03-11"]["day_ahead"] == app.day_logs["2026-03-11"]["day_ahead"]
    assert [d["date"] for d in app2.history_index()["days"]] == ["2026-03-09", "2026-03-10", "2026-03-11"]


async def test_history_api(tmpdata, aiohttp_client):
    fake = FakeHA(utc(2026, 3, 11, 12, 0))
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    await app.step()
    app.history.write(H.build_day(date(2026, 3, 9), TZ, [], None))
    client = await aiohttp_client(make_web(app, None))
    r = await client.get("/api/history")
    assert r.status == 200
    idx = await r.json()
    assert idx["today"] == "2026-03-11" and [d["date"] for d in idx["days"]] == ["2026-03-09", "2026-03-11"]
    r = await client.get("/api/history/2026-03-09")
    assert r.status == 200 and (await r.json())["date"] == "2026-03-09"
    r = await client.get("/api/history/2026-03-11")
    assert r.status == 200 and "live" in await r.json()
    for bad in ("2026-03-08", "2026-02-30", "..%2Fstate", "2026-3-9", "%2E%2E"):
        assert (await client.get("/api/history/" + bad)).status == 404, bad
    outside = await aiohttp_client(make_web(app, {"172.30.32.2"}))   # only the ingress proxy may connect
    for path in ("/", "/api/status", "/api/history", "/api/history/2026-03-09"):
        assert (await outside.get(path)).status == 403, path


def _march_prices(price_at):
    day = datetime(2026, 3, 10, tzinfo=TZ)
    return [{"time": (day + timedelta(hours=h)).isoformat(), "price": price_at(h % 24, h // 24)} for h in range(48)]


async def test_a_higher_peak_this_month_is_used_for_free_and_announced(tmpdata):
    """The month already has a 3.6 kW quarter-hour: at a negative price the plan charges up to it."""
    prices = _march_prices(lambda h, d: -0.2 if d == 0 and h in (12, 13) else 0.1)
    fake = FakeHA(utc(2026, 3, 10, 10, 0), soc=30, prices=prices)
    app = App(Options(contract="dynamic"), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    assert app.plan.boost is None                               # nothing measured this month yet
    app.peaks.months["2026-03"] = 3.6
    app.replan()
    boost = app.plan.boost
    assert boost and app.o.import_limit_kw < boost["limit_kw"] <= 3.6 and boost["peak_cost_eur"] == 0
    assert any(e["text"].startswith("Plan: charging above the import limit") for e in app.events)
    await app.publish()
    assert fake.published["sensor.battery_pilot_plan"]["attributes"]["boost_kw"] == boost["limit_kw"]


async def test_no_peak_warning_while_charging_above_the_limit_on_purpose(tmpdata):
    fake = FakeHA(utc(2026, 3, 10, 12, 0))
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    await app.tick()
    alerts = lambda: [c for c in fake.calls if c[0] == "persistent_notification"   # noqa: E731
                      and c[2].get("title") == "Capacity peak warning"]
    for s in app.plan.steps:
        s.limit_kw = 3.45
    for minute in range(4):
        await app._peak_warning(fake.now + timedelta(minutes=minute), 3.3)
    assert not alerts()                                         # 3.3 kW is the peak the plan paid for
    for minute in range(4, 8):
        await app._peak_warning(fake.now + timedelta(minutes=minute), 3.9)
    assert len(alerts()) == 1                                   # above it, the warning still comes


async def test_nightly_replay_charges_above_the_limit_at_an_extreme_price(tmpdata):
    day = datetime(2026, 3, 10, tzinfo=TZ)
    start = day.astimezone(timezone.utc)
    prices = _march_prices(lambda h, d: -8.0 if d == 0 and h == 13 else 0.1)
    fake = FakeHA(start - timedelta(minutes=5), soc=30, prices=prices)
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    for q in range(96):
        t = start + timedelta(minutes=15 * q)
        app.records.append(QuarterRecord(t, {"import": 0.1, "export": 0.0, "load": 0.1, "pv": 0.0},
                                         {"import": 1, "export": 1, "load": 1, "pv": 1}, 30))
    fake.now = start + timedelta(days=1, minutes=30)
    await app.nightly()
    replay = app.comparison.months["2026-03"]["replay"]
    assert replay["dynamic"]["peak_kw"] > Options().import_limit_kw + 0.5    # paid to take energy: worth a peak
    assert replay["fixed"]["peak_kw"] <= Options().import_limit_kw + 0.1     # the fixed contract has no reason
