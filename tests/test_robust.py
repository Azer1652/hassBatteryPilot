"""Gaps in what Home Assistant provides: missing prices, a forecast outage, a slow answer,
and alerts on days the app was not running."""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fake_ha import TZ, FakeHA, utc

from pilot.app import App
from pilot.compare import Comparison
from pilot.config import Options
from pilot.meter import QuarterRecord
from pilot.prices import PriceSlot


@pytest.fixture
def tmpdata(tmp_path):
    return str(tmp_path / "data")


def sunny(day, kwh):
    return {datetime(day.year, day.month, day.day, h, tzinfo=TZ).astimezone(timezone.utc): kwh / 10 for h in range(7, 17)}


async def started(tmpdata, fake, **opts):
    app = App(Options(**opts), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    return app


# ---------------------------------------------------------------- missing prices
async def test_quarters_without_a_market_price_are_priced_like_the_day_before(tmpdata):
    """Fixed counted every kWh, dynamic only the priced quarters: dynamic looked cheaper."""
    fake = FakeHA(utc(2026, 3, 11, 12, 0))
    app = await started(tmpdata, fake)
    start = datetime(2026, 3, 9, tzinfo=TZ).astimezone(timezone.utc)
    full = [PriceSlot(start + timedelta(hours=h), start + timedelta(hours=h + 1), 60 + 8 * (h % 24)) for h in range(48)]
    gap = (datetime(2026, 3, 10, 12, tzinfo=TZ).astimezone(timezone.utc),
           datetime(2026, 3, 11, tzinfo=TZ).astimezone(timezone.utc))

    def month(slots):
        app.comparison, app.price_slots = Comparison(), slots
        for q in range(96):
            t = datetime(2026, 3, 10, tzinfo=TZ).astimezone(timezone.utc) + timedelta(minutes=15 * q)
            app._quarter_closed(QuarterRecord(t, {"import": 0.25, "export": 0.0, "load": 0.25, "pv": 0.0},
                                              {"import": 1, "export": 1, "load": 1, "pv": 1}, 50))
        return app.comparison.report("2026-03", app.o)

    priced = month(full)
    estimated = month([p for p in full if not gap[0] <= p.start < gap[1]])
    assert estimated["same_usage"]["dynamic_eur"] == pytest.approx(priced["same_usage"]["dynamic_eur"], rel=0.02)
    assert estimated["estimated_quarters"] == 48 and priced["estimated_quarters"] == 0


async def test_a_dynamic_contract_without_prices_waits_in_self_use(tmpdata):
    fake = FakeHA(utc(2026, 6, 14, 12, 0), soc=60)
    fake.s["input_boolean.battery_pilot_control"]["state"] = "on"
    app = await started(tmpdata, fake, contract="dynamic")              # no price sensor at all
    assert app.plan is None and app.problem.startswith("Dynamic contract but no prices")
    await app.step()
    assert app.decision.command in ("self_use", "none") and fake.s["switch.solis_time_of_use"]["state"] == "off"


async def test_prices_that_cannot_be_read_keep_the_ones_already_known(tmpdata):
    day = datetime(2026, 6, 14, tzinfo=TZ)
    prices = [{"time": (day + timedelta(hours=h)).isoformat(), "price": 0.1} for h in range(48)]
    fake = FakeHA(utc(2026, 6, 14, 12, 0), prices=prices)
    app = await started(tmpdata, fake, contract="dynamic")
    known = list(app.price_slots)
    real = fake.state

    async def broken(eid):
        if eid == "sensor.current_electricity_market_price":
            raise TimeoutError("no answer")
        return await real(eid)

    fake.state = broken
    await app.refresh()
    assert app.price_slots == known and app.plan is not None
    assert any("Reading prices failed" in e["text"] for e in app.events)


def _evening_dear(first_day, days, minutes):
    """Price list (EUR/kWh) from `first_day`: 0.05 until 17:00, 0.20 after. The day's average is 93.75 EUR/MWh."""
    return [{"time": (first_day + timedelta(minutes=minutes * k)).isoformat(),
             "price": 0.05 if (k * minutes // 60) % 24 < 17 else 0.20} for k in range(days * 24 * 60 // minutes)]


@pytest.mark.parametrize("minutes", [15, 60])
async def test_a_days_market_average_is_not_redone_from_what_is_left_of_the_day(tmpdata, minutes):
    """Prices are kept for 3 days. While a day was being dropped, its average was taken again from
    the hours still there: its dear evening. With quarter-hour prices 93.75 became 137.5, then 200.
    That average sets the fixed contract's sell price and the break-even alerts."""
    day = datetime(2026, 11, 5, tzinfo=TZ)
    fake = FakeHA(utc(2026, 11, 5, 12, 0), prices=_evening_dear(day, 2, minutes))
    app = await started(tmpdata, fake)
    assert app.daily_market == {"2026-11-05": 93.75}                   # (tomorrow is not a day yet)
    for hour, minute in ((3, 0), (12, 0), (18, 30), (19, 30)):
        fake.now = utc(2026, 11, 8, hour, minute)
        fake.s["sensor.current_electricity_market_price"]["attributes"]["prices"] = \
            _evening_dear(day + timedelta(days=3), 2, minutes)       # the sensor lists today and tomorrow
        await app.refresh()
        assert app.daily_market["2026-11-05"] == 93.75
        assert app.daily_market["2026-11-08"] == 93.75
        assert app.market_avg_month(fake.now) == pytest.approx(93.75)


@pytest.mark.parametrize("minutes,marked,kept", [(15, False, False), (60, False, True), (15, True, True)])
async def test_averages_taken_from_parts_of_days_are_dropped_once(tmpdata, minutes, marked, kept):
    """A state saved before the fix, with quarter-hour prices: its daily averages are the evenings."""
    day = datetime(2026, 11, 5, tzinfo=TZ)
    fake = FakeHA(utc(2026, 11, 5, 12, 0), prices=_evening_dear(day, 2, minutes))
    app = await started(tmpdata, fake)
    app.save_state()
    with open(app.state_file, encoding="utf-8") as f:
        state = json.load(f)
    assert state["whole_day_averages"] is True
    state["daily_market"] = {"2026-11-01": 200.0, "2026-11-02": 200.0}
    if not marked:
        del state["whole_day_averages"]
    with open(app.state_file, "w", encoding="utf-8") as f:
        json.dump(state, f)
    again = await started(tmpdata, fake)
    assert ("2026-11-01" in again.daily_market) is kept
    assert again.daily_market["2026-11-05"] == 93.75                   # rebuilt from the prices still stored


# ---------------------------------------------------------------- the solar forecast
async def test_daily_forecast_totals_are_used_without_an_energy_dashboard_forecast(tmpdata):
    fake = FakeHA(utc(2026, 6, 14, 20, 0))
    fake.s["sensor.energy_production_today"] = {"state": "20", "attributes": {"unit_of_measurement": "kWh"}}
    fake.s["sensor.energy_production_tomorrow"] = {"state": "25", "attributes": {"unit_of_measurement": "kWh"}}

    async def no_energy_forecast(*commands):
        raise RuntimeError("energy/solar_forecast: not set up")

    fake.ws = no_energy_forecast
    app = await started(tmpdata, fake)
    assert app.pv.day_total(date(2026, 6, 15), corrected=False) == pytest.approx(25, abs=0.1)
    assert not app.plan.winter


async def test_a_forecast_outage_keeps_the_last_forecast(tmpdata):
    """A night with no forecast counted as a winter night: the battery was grid-charged all night."""
    fc = {**sunny(date(2026, 6, 14), 30), **sunny(date(2026, 6, 15), 30)}
    fake = FakeHA(utc(2026, 6, 14, 20, 0), forecast=fc)
    app = await started(tmpdata, fake)
    assert not app.plan.winter
    fake.forecast = {}                                           # the forecast service is down
    for _ in range(2):
        await app.refresh()
        assert not app.plan.winter and app.pv.day_total(date(2026, 6, 15)) > 20
    assert len([e for e in app.events if "solar forecast is unavailable" in e["text"]]) == 1


async def test_with_no_forecast_ever_every_night_is_a_winter_night(tmpdata):
    """As documented: without any solar forecast, there is no sun in the plan."""
    app = await started(tmpdata, FakeHA(utc(2026, 6, 14, 20, 0)))
    assert app.plan.winter


# ---------------------------------------------------------------- a slow Home Assistant
async def test_a_slow_forecast_does_not_hold_up_the_control_loop(tmpdata):
    fake = FakeHA(utc(2026, 6, 14, 12, 0), forecast=sunny(date(2026, 6, 15), 30))
    app = await started(tmpdata, fake)
    real = fake.ws

    async def slow(*commands):
        await asyncio.sleep(1.0)                                 # the websocket can take up to a minute
        return await real(*commands)

    fake.ws = slow
    app._last["refresh"] = None
    await asyncio.wait_for(app.step(), 0.3)                      # the refresh runs in the background
    assert not app._refresh_task.done()
    fake.advance(timedelta(seconds=10))
    await asyncio.wait_for(app.step(), 0.3)                      # and the next tick is not held up
    await app._refresh_task
    assert app.plan.created == fake.now - timedelta(seconds=10)


# ---------------------------------------------------------------- alerts
def alerts(fake, title):
    return [c for c in fake.calls if c[0] == "persistent_notification" and c[2].get("title", "").startswith(title)]


async def test_reminders_and_the_monthly_verdict_catch_up_after_days_off(tmpdata):
    fake = FakeHA(utc(2026, 11, 12, 9, 30))                     # 41 days before the end: the 42-day day was missed
    app = await started(tmpdata, fake, fixed_contract_end="2026-12-23")
    await app.daily_alerts()
    await app.daily_alerts()
    assert [c[2]["title"] for c in alerts(fake, "Fixed price ends")] == ["Fixed price ends in 41 days"]
    app.comparison.month("2026-11")["imp_day"] = 100
    fake.now = utc(2026, 12, 2, 9, 30)                           # the app was off on 1 December
    await app.daily_alerts()
    await app.daily_alerts()
    assert len(alerts(fake, "Contract check 2026-11")) == 1
    fake.now = utc(2026, 12, 10, 9, 30)                          # 13 days: the 14-day reminder, once
    await app.daily_alerts()
    await app.daily_alerts()
    assert [c[2]["title"] for c in alerts(fake, "Fixed price ends")][1:] == ["Fixed price ends in 13 days"]


async def test_market_break_even_alerts_once_per_crossing(tmpdata):
    fake = FakeHA(utc(2026, 3, 31, 9, 30))
    app = await started(tmpdata, fake)
    app.daily_market = {(date(2026, 3, 1) + timedelta(days=d)).isoformat(): 40.0 for d in range(30)}
    await app.daily_alerts()
    await app.daily_alerts()
    assert len(alerts(fake, "Market below your fixed night rate")) == 1
    app.daily_market = {k: 400.0 for k in app.daily_market}
    await app.daily_alerts()
    await app.daily_alerts()
    assert len(alerts(fake, "Market above your fixed day rate")) == 1


async def test_a_dynamic_price_that_does_not_follow_the_market_has_no_break_even(tmpdata):
    """Multiplier 0 was a division by zero: no dashboard, and the daily alerts failed every round."""
    fake = FakeHA(utc(2026, 3, 31, 9, 30))
    app = await started(tmpdata, fake, dynamic_buy_mult=0)
    app.daily_market = {(date(2026, 3, 1) + timedelta(days=d)).isoformat(): 40.0 for d in range(30)}
    await app.daily_alerts()
    assert app.status()["breakeven"] == {"night": None, "day": None}
    assert not alerts(fake, "Market below your fixed night rate")


async def test_a_day_replayed_for_one_contract_only_is_not_added_to_the_month(tmpdata):
    """Without prices only the fixed contract is replayed. Those days were added to the fixed side of
    the month, and not counted: a later day with prices then compared 2 days of fixed with 1 of dynamic."""
    fake = FakeHA(utc(2026, 3, 11, 12, 0))
    app = await started(tmpdata, fake)
    one = {"cost_c": 300.0, "imp": 10.0, "exp": 0.0, "charged": 2.0, "discharged": 2.0, "grid_charged": 0.0,
           "peak_kw": 2.0, "soc_end": 40.0}
    app._apply_replay(date(2026, 3, 9), {"fixed": dict(one)})
    m = app.comparison.month("2026-03")
    assert m["replay_days"] == 0 and m["replay"]["fixed"]["cost_c"] == 0
    assert app.comparison.sim_soc == {"fixed": 40.0}                   # its simulated battery still carries over
    app._apply_replay(date(2026, 3, 10), {"fixed": dict(one), "dynamic": dict(one, cost_c=250.0)})
    assert m["replay_days"] == 1
    assert (m["replay"]["fixed"]["cost_c"], m["replay"]["dynamic"]["cost_c"]) == (300.0, 250.0)


async def test_the_monthly_verdict_includes_the_best_battery_use(tmpdata):
    fake = FakeHA(utc(2026, 3, 1, 9, 30))
    app = await started(tmpdata, fake)
    m = app.comparison.month("2026-02")
    m["imp_day"], m["replay_days"] = 120, 20
    m["replay"]["fixed"]["cost_c"], m["replay"]["dynamic"]["cost_c"] = 4000, 3500
    await app.daily_alerts()
    text = alerts(fake, "Contract check 2026-02")[0][2]["message"]
    assert "best battery use for each (20 days)" in text


# ---------------------------------------------------------------- the ENTSO-e price list
def _entsoe(fake, day):
    """ENTSO-e as it is: the list is an attribute of the average sensor, the current one has none."""
    from fake_ha import st
    prices = [{"time": str(day + timedelta(hours=h)), "price": 0.1} for h in range(48)]
    fake.s["sensor.current_electricity_market_price"] = st(0.1, "EUR/kWh")
    fake.s["sensor.average_electricity_price"] = st(0.1, "EUR/kWh", prices=prices)


async def test_the_entsoe_sensor_with_the_price_list_is_the_one_found(tmpdata):
    """The app took 'current electricity market price', which has no list: no dynamic prices and a
    comparison that stayed on 'collecting data', with nothing in the log."""
    fake = FakeHA(utc(2026, 11, 11, 12, 0))
    _entsoe(fake, datetime(2026, 11, 11, tzinfo=TZ))
    app = await started(tmpdata, fake)
    assert app.ent_ids["price_sensor"] == "sensor.average_electricity_price"
    await app.refresh()
    assert len(app.price_slots) == 48 and app.price_slots[0].eur_mwh == 100


async def test_a_setting_that_names_the_entsoe_current_sensor_is_followed_to_the_list(tmpdata):
    fake = FakeHA(utc(2026, 11, 11, 12, 0))
    _entsoe(fake, datetime(2026, 11, 11, tzinfo=TZ))
    app = await started(tmpdata, fake, entities={"price_sensor": "sensor.current_electricity_market_price"})
    await app.refresh()
    assert len(app.price_slots) == 48
    assert not any("no price list" in e["text"] for e in app.events)


async def test_a_price_sensor_without_a_price_list_is_reported_once(tmpdata):
    from fake_ha import st
    fake = FakeHA(utc(2026, 11, 11, 12, 0))
    fake.s["sensor.nordpool_be"] = st(0.1, "EUR/kWh")
    app = await started(tmpdata, fake)
    await app.refresh()
    await app.refresh()
    assert app.price_slots == []
    assert sum("no price list" in e["text"] for e in app.events) == 1
