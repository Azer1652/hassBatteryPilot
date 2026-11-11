"""Whole days through the app, the controller and the simulated Solis: exporting, an extreme
price, a disappointing sun, the DST nights, and learning from the recorder on the first start."""
import math
from datetime import date, datetime, timedelta, timezone

import pytest
from fake_ha import CHEAP_NIGHT, TZ, FakeHA, utc

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
    app = await start(tmpdata, fake, weekend_is_night=False, **CHEAP_NIGHT)   # (both nights are Saturdays)
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


# ---------------------------------------------------------------- peak shaving
SHAVE = "switch.solis_peak_shaving_mode"


async def shaving_night(tmpdata, **opts):
    """A winter night on the default rates, a nearly empty battery: it is kept for the day."""
    day = date(2026, 1, 14)
    fake = FakeHA(utc(2026, 1, 13, 21, 30), soc=16, load=lambda t: 0.3, peak_shaving=True,
                  forecast={**dull(day), **dull(day + timedelta(days=1))})
    log = recorded(fake)
    app = await start(tmpdata, fake, **opts)
    await run(app, fake, utc(2026, 1, 13, 23, 0))
    return app, fake, log


async def test_a_winter_night_in_peak_shaving_keeps_the_battery_for_the_day(tmpdata):
    app, fake, log = await shaving_night(tmpdata)
    assert not app.plan.targets[0]["applied"] and not app.plan.targets[0]["pays"]      # no night filling
    assert app.solis.mode == "peak_shave" and fake.mode() == "peak_shave"
    assert fake.s["number.solis_peak_max_usable_grid_power"]["state"] == "2300.0"      # the import limit, in W
    assert fake.s["number.solis_peak_baseline_soc"]["state"] == "10.0"                 # never refilled by the inverter
    assert fake.s["switch.solis_time_of_use"]["state"] == "off"
    soc = fake.soc
    await run(app, fake, utc(2026, 1, 14, 6, 55))
    assert fake.soc == pytest.approx(soc, abs=0.5)                # the grid fed the house all night
    assert fake.import_kw == pytest.approx(0.3, abs=0.05)
    assert all(s.action != "charge" for s in app.plan.steps)
    await run(app, fake, utc(2026, 1, 14, 8, 0))
    assert fake.mode() == "self_use" and fake.s[SHAVE]["state"] == "off"               # the day rate: used now
    assert fake.soc < soc - 1
    numbers = [d for _, dom, _, d in log if dom == "number" and "peak" in d["entity_id"]]
    assert len(numbers) == 2                                      # limit and SOC are written once, not every hour
    assert not [e for e in app.events if e["text"].startswith("Error")]


async def test_peak_shaving_is_ended_when_the_app_stops_or_control_goes_off(tmpdata):
    app, fake, _ = await shaving_night(tmpdata)
    await app.shutdown()
    assert fake.s[SHAVE]["state"] == "off" and app.solis.mode == "self_use"

    app, fake, _ = await shaving_night(tmpdata)
    fake.s[CONTROL]["state"] = "off"
    await run(app, fake, fake.now + timedelta(minutes=2))
    assert fake.s[SHAVE]["state"] == "off"


async def test_a_crash_while_peak_shaving_is_cleaned_up_at_the_next_start(tmpdata):
    app, fake, _ = await shaving_night(tmpdata)
    assert fake.s[SHAVE]["state"] == "on"
    fake.s[CONTROL]["state"] = "off"                      # (so the new start does not switch it on again)
    app2 = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app2.setup()
    assert fake.s[SHAVE]["state"] == "off"
    assert any("set back to self-use" in e["text"] for e in app2.events)


async def test_peak_shaving_is_ended_when_the_measurements_are_lost(tmpdata):
    app, fake, _ = await shaving_night(tmpdata)
    fake.override["sensor.solis_battery_soc"] = "unavailable"
    await run(app, fake, fake.now + timedelta(minutes=3))
    assert fake.s[SHAVE]["state"] == "off"


async def test_a_peak_shaving_switch_changed_by_something_else_is_corrected(tmpdata):
    app, fake, _ = await shaving_night(tmpdata)
    fake.s[SHAVE]["state"] = "off"                        # someone switched it off
    await run(app, fake, fake.now + timedelta(minutes=3))
    assert fake.s[SHAVE]["state"] == "on"
    await run(app, fake, utc(2026, 1, 14, 8, 0))
    assert fake.s[SHAVE]["state"] == "off"
    fake.s[SHAVE]["state"] = "on"                         # and on, while the plan is self-use
    await run(app, fake, fake.now + timedelta(minutes=3))
    assert fake.s[SHAVE]["state"] == "off"


async def test_without_the_peak_shaving_switch_nothing_changes(tmpdata):
    fake = FakeHA(utc(2026, 1, 13, 21, 30), soc=16, load=lambda t: 0.3,
                  forecast={**dull(date(2026, 1, 14)), **dull(date(2026, 1, 15))})
    app = await start(tmpdata, fake)
    await run(app, fake, utc(2026, 1, 13, 23, 0))
    assert not app.can_peak_shave and app.solis.mode == "self_use"
    assert all(s.action != "peak_shave" for s in app.plan.steps)
