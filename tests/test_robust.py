"""Gaps in what Home Assistant provides: missing prices, a forecast outage, a slow answer,
and alerts on days the app was not running."""
import asyncio
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


async def test_the_monthly_verdict_includes_the_best_battery_use(tmpdata):
    fake = FakeHA(utc(2026, 3, 1, 9, 30))
    app = await started(tmpdata, fake)
    m = app.comparison.month("2026-02")
    m["imp_day"], m["replay_days"] = 120, 20
    m["replay"]["fixed"]["cost_c"], m["replay"]["dynamic"]["cost_c"] = 4000, 3500
    await app.daily_alerts()
    text = alerts(fake, "Contract check 2026-02")[0][2]["message"]
    assert "best battery use for each (20 days)" in text
