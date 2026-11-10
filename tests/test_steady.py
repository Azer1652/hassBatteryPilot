"""Steady inverter commands: the plan is redone every few minutes from the measured SOC, but
the inverter must not be switched back and forth (each switch is a full command sequence)."""
import math
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from fake_ha import TZ, FakeHA, utc

from pilot.app import App
from pilot.config import Options

CONTROL = "input_boolean.battery_pilot_control"
TOU = "switch.solis_time_of_use"


@pytest.fixture
def tmpdata(tmp_path):
    return str(tmp_path / "data")


def sun(day, kwh, first=6, last=20):
    return {datetime(day.year, day.month, day.day, h, tzinfo=TZ).astimezone(timezone.utc):
            kwh * math.sin(math.pi * (h + 0.5 - first) / (last - first)) / 8.9 for h in range(first, last)}


async def run(tmpdata, start, end, soc, prices, kwh, load, contract="dynamic", step_s=30):
    """Run the app with control on; returns (app, fake, the time-of-use writes with their local hour)."""
    day = start.astimezone(TZ).replace(hour=0, minute=0)
    fc = {}
    for i in range(3):
        fc.update(sun(day + timedelta(days=i), kwh))
    price_list = [{"time": (day + timedelta(hours=h)).isoformat(), "price": prices(h % 24)} for h in range(72)]
    fake = FakeHA(start, soc=soc, pv=lambda t: fc.get(t.replace(minute=0, second=0, microsecond=0), 0.0),
                  forecast=fc, prices=price_list, load=load)
    fake.s[CONTROL]["state"] = "on"
    writes = []
    real = fake.call

    async def call(domain, service, data=None):
        if (data or {}).get("entity_id") == TOU:
            writes.append((fake.now.astimezone(TZ).strftime("%d %H"), service))
        return await real(domain, service, data)

    fake.call = call
    app = App(Options(contract=contract), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    lo, hi = 100.0, 0.0                                   # lowest SOC while exporting, highest overall
    while fake.now < end:
        await app.step()
        if fake.mode() == "discharge":
            lo = min(lo, fake.soc)
        hi = max(hi, fake.soc)
        fake.advance(timedelta(seconds=step_s))
    return app, fake, writes, (lo, hi)


def switches(writes):
    """On/off changes of the time-of-use switch (renewing a charge or discharge each hour is not one)."""
    return sum(1 for a, b in zip(writes, writes[1:]) if a[1] != b[1])


def most_switches_in_an_hour(writes):
    """On/off changes of the time-of-use switch per clock hour (renewing a charge is not a change)."""
    per_hour, last = Counter(), None
    for hour, service in writes:
        if last is not None and service != last:
            per_hour[hour] += 1
        last = service
    return max(per_hour.values(), default=0)


def evening(h):
    return 0.21 if 18 <= h < 22 else 0.02 if 11 <= h < 17 else 0.095


async def test_an_evening_export_is_not_switched_back_and_forth(tmpdata):
    """It flipped 7 times in one hour before the switch cost."""
    app, fake, writes, (lo, _) = await run(tmpdata, utc(2026, 6, 14, 17, 30), utc(2026, 6, 14, 23, 0), 90, evening, 35,
                                           lambda t: 0.45)
    assert most_switches_in_an_hour(writes) <= 2
    assert switches(writes) <= 3                                    # one start and one stop for the evening
    assert lo >= Options().battery_reserve_soc - 1                  # never exported past the reserve
    assert any(c[1] == "set_value" and "discharge_current" in c[2]["entity_id"] for c in fake.calls)   # it did sell


async def test_prices_near_break_even_do_not_make_the_inverter_hop(tmpdata):
    _, _, writes, _ = await run(tmpdata, utc(2026, 6, 14, 6, 0), utc(2026, 6, 14, 18, 0), 50,
                                lambda h: [0.09, 0.11, 0.10, 0.12, 0.095, 0.115][h % 6], 20, lambda t: 0.45)
    assert most_switches_in_an_hour(writes) <= 2
    assert switches(writes) <= 6


async def test_a_wobbling_house_load_lowers_the_charge_current_instead_of_pausing(tmpdata):
    """Cooking on and off every two minutes during a winter night charge."""
    wobble = lambda t: 0.8 if (t.minute // 2) % 2 == 0 else 1.1   # noqa: E731
    app, fake, writes, (_, hi) = await run(tmpdata, utc(2026, 1, 14, 22, 0), utc(2026, 1, 15, 7, 0), 20,
                                           lambda h: 0.1, 3, wobble, contract="fixed")
    assert most_switches_in_an_hour(writes) <= 2                    # was 4
    assert switches(writes) <= 6                                    # was 9
    target = max(t["soc"] for t in app.plan.targets) if app.plan.targets else 100
    assert hi <= target + 3                                         # and never charged on past the target
    assert any(c[2].get("entity_id") == "number.solis_time_charging_charge_current" for c in fake.calls[5:])


# ---------------------------------------------------------------- boost and the first plan
async def negative_noon_with_a_high_month_peak(tmpdata, end):
    day = datetime(2026, 3, 10, tzinfo=TZ)
    prices = [{"time": (day + timedelta(hours=h)).isoformat(), "price": -0.2 if h in (12, 13) else 0.1}
              for h in range(48)]
    fake = FakeHA(utc(2026, 3, 10, 10, 0), soc=30, prices=prices)
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(contract="dynamic"), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    app.peaks.months["2026-03"] = 3.6
    app.replan()
    while fake.now < end:
        await app.step()
        fake.advance(timedelta(seconds=30))
    return app, fake


async def test_a_boost_is_announced_once_and_stays_within_the_paid_peak(tmpdata):
    app, fake = await negative_noon_with_a_high_month_peak(tmpdata, utc(2026, 3, 10, 14, 30))
    announced = [e for e in app.events if e["text"].startswith("Plan: charging above the import limit")]
    assert len(announced) == 1                                       # was 16: once per 5-minute replan
    assert app.peaks.month_peak(fake.now, TZ) <= 3.6 + 0.15          # the meter agrees with what was paid for


async def test_a_restart_during_a_boost_keeps_charging_above_the_limit_without_a_new_announcement(tmpdata):
    app, fake = await negative_noon_with_a_high_month_peak(tmpdata, utc(2026, 3, 10, 12, 20))
    app.save_state()
    app2 = App(Options(contract="dynamic"), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app2.setup()
    assert app2.plan.boost                                           # the month's peak was saved
    end = utc(2026, 3, 10, 12, 40)
    above = 0
    while fake.now < end:
        await app2.step()
        above += fake.import_kw > Options().import_limit_kw + 0.3
        fake.advance(timedelta(seconds=30))
    assert above > 10
    assert not [e for e in app2.events if e["text"].startswith("Plan: charging above the import limit")]


async def test_the_peak_warning_uses_the_import_limit_outside_the_boost(tmpdata):
    app, fake = await negative_noon_with_a_high_month_peak(tmpdata, utc(2026, 3, 10, 10, 5))
    outside = [s for s in app.plan.steps if s.action != "charge"]
    assert outside and all(s.limit_kw is None for s in outside)


async def test_the_first_plan_after_a_start_uses_the_real_soc(tmpdata):
    fake = FakeHA(utc(2026, 1, 14, 12, 0), soc=12)
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    assert app.plan.steps[0].soc_start == pytest.approx(12, abs=1)   # was 50: no reading yet
