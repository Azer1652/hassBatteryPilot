"""Whole days through the app, the controller and the simulated Solis: exporting, an extreme
price, a disappointing sun, the DST nights, and learning from the recorder on the first start."""
import math
from datetime import date, datetime, timedelta, timezone

import pytest
from fake_ha import TZ, FakeHA, utc

from pilot.app import App
from pilot.config import Options

CONTROL = "input_boolean.battery_pilot_control"


@pytest.fixture
def tmpdata(tmp_path):
    return str(tmp_path / "data")


def sun(day, kwh, first=6, last=20):
    return {datetime(day.year, day.month, day.day, h, tzinfo=TZ).astimezone(timezone.utc):
            kwh * math.sin(math.pi * (h + 0.5 - first) / (last - first)) / 8.9 for h in range(first, last)}


def dull(day, kwh=2):
    return {datetime(day.year, day.month, day.day, h, tzinfo=TZ).astimezone(timezone.utc): kwh / 7 for h in range(9, 16)}


def hourly_prices(day, fn, days=3):
    return [{"time": (datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=h)).isoformat(),
             "price": fn(h // 24, h % 24)} for h in range(24 * days)]


async def run(app, fake, end, step_s=30, each=None):
    while fake.now < end:
        await app.step()
        if each:
            each()
        fake.advance(timedelta(seconds=step_s))


def recorded(fake):
    """Wrap the fake's service calls to keep (local time, domain, service, data)."""
    log, real = [], fake.call

    async def call(domain, service, data=None):
        log.append((fake.now.astimezone(TZ), domain, service, data or {}))
        return await real(domain, service, data)

    fake.call = call
    return log


async def start(tmpdata, fake, **opts):
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(**opts), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    return app


# ---------------------------------------------------------------- exporting
async def test_an_evening_export_through_the_solis(tmpdata):
    prices = hourly_prices(date(2026, 6, 14), lambda d, h: 0.30 if d == 0 and h in (19, 20) else 0.10)
    fake = FakeHA(utc(2026, 6, 14, 18, 55), soc=90, prices=prices, pv=lambda t: 0.0,
                  forecast={**sun(date(2026, 6, 15), 30), **sun(date(2026, 6, 16), 30)})
    log = recorded(fake)
    app = await start(tmpdata, fake, contract="dynamic")
    lowest, planned = [100.0], {}

    def watch():
        if fake.mode() == "discharge":
            lowest[0] = min(lowest[0], fake.soc)
        local = fake.now.astimezone(TZ)
        if local.minute == 0 and local.second == 0 and app.plan:
            step = app.plan.step_at(fake.now)
            planned[local.hour] = step.soc_end if step else None

    await run(app, fake, utc(2026, 6, 14, 21, 30), each=watch)
    slots = {(t.hour, d["entity_id"].split("time_charging_")[1]): d["time"] for t, dom, _, d in log if dom == "time"}
    assert slots[(19, "discharge_start_slot_1")] == "19:00:00" and slots[(19, "discharge_end_slot_1")] == "20:00:00"
    assert slots[(19, "charge_start_slot_1")] == slots[(19, "charge_end_slot_1")] == "00:00:00"   # no charging
    assert slots[(20, "discharge_start_slot_1")] == "20:00:00"                                  # renewed each hour
    assert lowest[0] >= Options().battery_reserve_soc - 1                                       # never past the reserve
    assert fake.mode() == "self_use"                                                            # done after 21:00
    assert fake.soc == pytest.approx(planned[20], abs=3)                                        # as the plan said


async def test_an_extreme_price_sells_below_the_reserve_through_the_solis(tmpdata):
    prices = hourly_prices(date(2026, 6, 14), lambda d, h: 8.0 if d == 0 and h == 19 else 0.10)
    fake = FakeHA(utc(2026, 6, 14, 18, 55), soc=60, prices=prices, pv=lambda t: 0.0,
                  forecast={**sun(date(2026, 6, 15), 30), **sun(date(2026, 6, 16), 30)})
    app = await start(tmpdata, fake, contract="dynamic")
    await run(app, fake, utc(2026, 6, 14, 19, 5))
    assert app.plan.step_at(fake.now).reserve == Options().battery_min_soc   # the step tells the controller
    await run(app, fake, utc(2026, 6, 14, 20, 1))
    # One hour at full power is about 28 points: as much as could be sold, below the 35 % reserve.
    assert fake.soc < Options().battery_reserve_soc and 60 - fake.soc >= 25


# ---------------------------------------------------------------- a disappointing sun
async def test_when_the_sun_disappoints_the_dear_evening_is_still_covered(tmpdata, monkeypatch):
    """The evening before, the battery was sold for a 35 kWh day; only half of it comes. The replans
    see the battery fill more slowly and keep it for the dear evening instead of selling more."""
    monkeypatch.setattr("pilot.app.REFRESH_S", 900)              # (a long day: replan every 15 minutes)
    fc = {**sun(date(2026, 6, 14), 35), **sun(date(2026, 6, 15), 35), **sun(date(2026, 6, 16), 35)}
    prices = hourly_prices(date(2026, 6, 14), lambda d, h: 0.21 if 18 <= h < 22 else 0.02 if 11 <= h < 17 else 0.095)
    fake = FakeHA(utc(2026, 6, 14, 17, 0), soc=90, prices=prices, forecast=fc, load=lambda t: 0.45,
                  pv=lambda t: fc.get(t.replace(minute=0, second=0, microsecond=0), 0.0) * 0.5)
    app = await start(tmpdata, fake, contract="dynamic")
    dear = [0.0]

    def watch():
        local = fake.now.astimezone(TZ)
        if local.day == 15 and 18 <= local.hour < 22:
            dear[0] += fake.import_kw * 60 / 3600

    await run(app, fake, utc(2026, 6, 15, 22, 0), step_s=60, each=watch)
    assert dear[0] < 0.1                                         # nothing bought in the dear evening
    assert app.peaks.month_peak(fake.now, TZ) <= Options().import_limit_kw + 0.1


# ---------------------------------------------------------------- the DST nights
@pytest.mark.parametrize("night,hours", [(date(2026, 3, 28), 23), (date(2026, 10, 24), 25)])
async def test_a_winter_night_charge_across_a_clock_change(tmpdata, night, hours):
    day = night + timedelta(days=1)
    fake = FakeHA(datetime.combine(night, datetime.min.time().replace(hour=22), TZ).astimezone(timezone.utc), soc=20,
                  forecast={**dull(day), **dull(day + timedelta(days=1))})
    log = recorded(fake)
    app = await start(tmpdata, fake)
    target = app.plan.targets[0]
    assert target["winter"]
    await run(app, fake, datetime.combine(day, datetime.min.time().replace(hour=7, minute=30), TZ).astimezone(timezone.utc),
              step_s=60)
    assert not [e for e in app.events if e["text"].startswith("Error")]
    assert fake.soc >= target["soc"] - 3
    for t, dom, _, d in log:
        if dom == "time" and d["entity_id"].endswith("charge_start_slot_1") and d["time"] != "00:00:00":
            assert d["time"] == t.strftime("%H:00:00")           # the slot is the local clock hour
    assert app.history_day(day)["minutes"] == hours * 60


# ---------------------------------------------------------------- the first start
async def test_the_first_start_learns_consumption_and_shade_from_the_recorder(tmpdata):
    now = utc(2026, 3, 11, 12, 0)
    fake = FakeHA(now)
    load, pv, fc = [], [], []
    t = now - timedelta(days=10)
    while t < now:
        h = t.astimezone(TZ).hour
        gap = t.date() == (now - timedelta(days=3)).date() and h == 5   # the recorder missed an hour
        load.append((t, "unavailable" if gap else str(1200 if 17 <= h < 21 else 400)))
        sun_w = max(math.sin(math.pi * (h + 0.5 - 7) / 11), 0) * 2000 if 7 <= h < 18 else 0.0
        fc.append((t, str(round(sun_w))))
        pv.append((t, str(round(sun_w * 0.6))))                 # the trees take 40 %
        t += timedelta(minutes=15)
    fake.history_data = {"sensor.solis_household_load_power": load, "sensor.solis_total_pv_power": pv,
                         "sensor.power_production_now": fc}
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    assert app.load.learned()
    assert app.load.daily_kwh() == pytest.approx(0.4 * 20 + 1.2 * 4, rel=0.1)
    assert app.shade.factor(3, 12) < 0.75
    assert any("Learned house consumption" in e["text"] for e in app.events)


async def test_a_recorder_that_cannot_be_read_does_not_stop_the_first_start(tmpdata):
    fake = FakeHA(utc(2026, 3, 11, 12, 0))

    async def broken(*a):
        raise RuntimeError("recorder unavailable")

    fake.history = broken
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    assert any("Could not read history" in e["text"] for e in app.events)
