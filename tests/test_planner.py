"""Battery simulator invariants and planner scenarios."""
import math
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from pilot.config import Options
from pilot.planner import Slot, make_plan, night_target, optimize
from pilot.prices import PriceSlot
from pilot.simulate import Battery, step
from pilot.solar import PvForecast, ShadeModel
from pilot.tariffs import DynamicTariff, FixedTariff

BXL = ZoneInfo("Europe/Brussels")
B = Battery()
T0 = datetime(2026, 1, 14, 0, 0, tzinfo=timezone.utc)


def hourly(buy, sell, load=0.5, pv=0.0):
    n = len(buy)
    load = load if isinstance(load, list) else [load] * n
    pv = pv if isinstance(pv, list) else [pv] * n
    return [Slot(T0 + timedelta(hours=i), 1.0, load[i], pv[i], buy[i], sell[i]) for i in range(n)]


# ---------------------------------------------------------------- simulator
@pytest.mark.parametrize("seed", range(20))
def test_energy_balance_and_limits(seed):
    rnd = random.Random(seed)
    soc = rnd.uniform(0, 100)
    for _ in range(50):
        action = rnd.choice(["self_use", "charge", "discharge"])
        load, pv, h = rnd.uniform(0, 3), rnd.uniform(0, 5), rnd.choice([0.25, 1.0])
        r = step(B, soc, action, rnd.uniform(0, 100), load * h, pv * h, h, curtail=rnd.random() < 0.2)
        lhs = load * h - pv * h + r.charge_dc_kwh / B.eta_c - r.discharge_dc_kwh * B.eta_d
        assert lhs == pytest.approx(r.import_kwh - r.export_kwh - r.curtailed_kwh, abs=1e-9)
        assert 0 <= r.soc <= 100
        assert r.charge_dc_kwh <= B.max_charge_kw * h + 1e-9
        assert r.discharge_dc_kwh <= B.max_discharge_kw * h + 1e-9
        assert r.export_kwh <= B.export_limit_kw * h + 1e-9
        if action == "self_use" and soc >= B.min_soc:
            assert r.soc >= B.min_soc - 1e-9
        soc = r.soc


def test_charging_follows_house_load_under_the_import_limit():
    r = step(B, 30, "charge", 90, load_kwh=1.0, pv_kwh=0, hours=1)
    assert r.import_kwh == pytest.approx(B.import_limit_kw)
    r = step(B, 30, "charge", 90, load_kwh=2.5, pv_kwh=0, hours=1)
    assert r.charge_dc_kwh == 0          # house alone is over the limit: no charging


def test_discharge_stops_at_reserve():
    r = step(B, 37, "discharge", 0, load_kwh=0.2, pv_kwh=0, hours=1)
    assert r.soc == pytest.approx(B.reserve_soc)


def test_a_plan_step_can_charge_above_the_limit_and_sell_below_the_reserve():
    r = step(B, 30, "charge", 90, load_kwh=1.0, pv_kwh=0, hours=1, limit_kw=3.5)
    assert r.import_kwh == pytest.approx(3.5)                         # follows the step's limit, not 2.3
    r = step(B, 37, "discharge", 0, load_kwh=0.2, pv_kwh=0, hours=1, reserve=B.min_soc)
    assert r.soc == pytest.approx(B.min_soc)


# ---------------------------------------------------------------- optimizer
def test_cheap_night_and_expensive_evening_on_dynamic():
    buy = [10] * 7 + [25] * 10 + [45] * 5 + [25] * 2
    sell = [2] * 7 + [8] * 10 + [35] * 5 + [8] * 2
    steps = optimize(B, hourly(buy, sell), soc0=20, terminal_c_kwh=15)
    acts = [s.action for s in steps]
    assert "charge" in acts[:7]
    assert "discharge" in acts[17:22]
    assert "charge" not in acts[17:22]      # never buys in the expensive hours


def test_no_arbitrage_when_the_spread_does_not_pay():
    # fixed day/night: (27.18 / 0.949 = 28.6 c per stored kWh) vs (30.55 x 0.949 - 2 wear = 27.0 c)
    buy = [27.18] * 7 + [30.55] * 15 + [27.18] * 2
    steps = optimize(B, hourly(buy, [5.0] * 24), soc0=50, terminal_c_kwh=24.6)
    assert all(s.action != "charge" for s in steps)
    assert all(s.action != "discharge" for s in steps)


def test_import_limit_and_reserve_are_respected():
    buy = [5] * 6 + [40] * 6 + [5] * 6 + [40] * 6
    sell = [1] * 6 + [30] * 6 + [1] * 6 + [30] * 6
    steps = optimize(B, hourly(buy, sell, load=1.0), soc0=50, terminal_c_kwh=10)
    for s in steps:
        if s.action == "charge":
            assert s.import_kwh / s.hours <= B.import_limit_kw + 1e-6
        if s.action == "discharge":
            assert s.soc_end >= B.reserve_soc
    assert any(s.action == "charge" for s in steps) and any(s.action == "discharge" for s in steps)


def test_negative_prices_charge_from_grid_and_block_export():
    buy = [25] * 10 + [-3] * 4 + [25] * 10
    sell = [8] * 10 + [-6] * 4 + [8] * 10
    pv = [0] * 9 + [1.2] * 6 + [0] * 9
    steps = optimize(B, hourly(buy, sell, load=0.4, pv=pv), soc0=15, curtail_negative=True, terminal_c_kwh=20)
    neg = steps[10:14]
    assert any(s.action == "charge" for s in neg)     # paid to take energy from the grid
    assert all(s.export_kwh == 0 for s in neg)        # and never pays to export


def test_negative_sell_price_without_curtailing_avoids_export_by_storing():
    buy = [25] * 10 + [10] * 4 + [25] * 10
    sell = [8] * 10 + [-6] * 4 + [8] * 10
    pv = [0] * 9 + [2.0] * 6 + [0] * 9
    steps = optimize(B, hourly(buy, sell, load=0.4, pv=pv), soc0=15, terminal_c_kwh=20)
    assert sum(s.export_kwh for s in steps[10:14]) == pytest.approx(0, abs=0.2)


def test_no_export_when_export_is_not_allowed():
    buy = [25] * 24
    sell = [8] * 17 + [40] * 3 + [8] * 4
    steps = optimize(B, hourly(buy, sell), soc0=90, allow_export=False, terminal_c_kwh=20)
    assert all(s.action != "discharge" for s in steps)


def test_spread_must_beat_losses_and_wear():
    # buying at 20 and selling at 21 loses money after 10 % losses and 2 c wear
    buy = [20] * 24
    sell = [5] * 18 + [21] + [5] * 5
    steps = optimize(B, hourly(buy, sell, load=0.0), soc0=B.reserve_soc, terminal_c_kwh=17)
    assert all(s.action == "self_use" for s in steps)


# ---------------------------------------------------------------- night target (regressions)
def test_target_reached_at_window_end_and_no_charge_after():
    buy = [27.18] * 7 + [30.55] * 15 + [27.18] * 2
    steps = optimize(B, hourly(buy, [5] * 24, load=0.4), soc0=30, targets={6: 80}, terminal_c_kwh=24.6)
    assert steps[6].soc_end >= 80
    assert all(s.action != "charge" for s in steps[7:22])


def test_bug1_no_charging_when_already_above_target():
    """Old playbook: SOC above target at window start still grid-charged to 100 %."""
    buy = [27.18] * 9 + [30.55] * 15
    steps = optimize(B, hourly(buy, [5] * 24, load=0.0), soc0=70, targets={8: 60}, terminal_c_kwh=24.6)
    assert all(s.action != "charge" for s in steps)
    assert max(s.soc_end for s in steps) <= 70


def test_bug2_night_drain_is_topped_up_before_the_window_ends():
    """Old playbook: target only checked at window start, the house drained it overnight."""
    buy = [27.18] * 9 + [30.55] * 15
    steps = optimize(B, hourly(buy, [5] * 24, load=0.5), soc0=62, targets={8: 60}, terminal_c_kwh=24.6)
    assert steps[8].soc_end >= 60
    assert any(s.action == "charge" for s in steps[:9])


def test_night_target_covers_the_day_plus_margin():
    o = Options()
    dull = [(0.9, 0.2, 1.0)] * 15          # 13.5 kWh used, 3 kWh solar
    target, need = night_target(B, o, dull)
    assert need == pytest.approx(B.kwh(math.ceil((13.5 - 3) / B.eta_d / 16 * 100 + 0.5)), abs=0.4)
    assert target == min(100, 5 * math.ceil((B.min_soc + need / 16 * 100 + o.safety_margin_pct) / 5))
    sunny = [(0.9, 3.0, 1.0)] * 15
    t2, need2 = night_target(B, o, sunny)
    assert need2 == 0 and t2 == 5 * math.ceil((B.min_soc + o.safety_margin_pct) / 5)


# ---------------------------------------------------------------- full plan from forecasts
def _bell(day_kwh):
    """Solar kWh between two times for a day total of day_kwh (08:00-16:00 local, sine)."""
    def pv(t0, t1):
        total, t = 0.0, t0
        while t < t1:
            h = t.astimezone(BXL).hour + t.astimezone(BXL).minute / 60
            if 8 <= h < 16:
                total += day_kwh * math.pi / 16 * math.sin(math.pi * (h - 8) / 8) * (1 / 12)
            t += timedelta(minutes=5)
        return total
    return pv


def _flat_load(kw):
    return lambda t0, t1: kw * (t1 - t0).total_seconds() / 3600


def test_winter_day_charges_at_night_on_fixed():
    now = datetime(2026, 1, 14, 20, 0, tzinfo=BXL).astimezone(timezone.utc)
    o = Options()
    plan = make_plan(now, BXL, o, FixedTariff(o), [], _flat_load(0.55), _bell(3.0), soc=25, market_avg=90)
    assert plan.winter and not plan.allow_export
    t = plan.targets[0]
    assert t["winter"] and t["soc"] >= 60
    end = datetime.fromisoformat(t["window_end"])
    at_end = [s for s in plan.steps if s.start + timedelta(hours=s.hours) <= end + timedelta(seconds=1)][-1]
    assert at_end.soc_end >= t["soc"]
    night = [s for s in plan.steps if s.start < end]
    assert any(s.action == "charge" for s in night)
    assert all(s.action != "charge" for s in plan.steps if s.start.astimezone(BXL).hour in range(7, 22)
               and s.start >= end)
    for s in plan.steps:
        if s.action == "charge":
            assert s.import_kwh / s.hours <= o.import_limit_kw + 1e-6


def test_sunny_summer_day_does_not_grid_charge_on_fixed():
    now = datetime(2026, 6, 14, 20, 0, tzinfo=BXL).astimezone(timezone.utc)
    o = Options()
    plan = make_plan(now, BXL, o, FixedTariff(o), [], _flat_load(0.55), _bell(30.0), soc=25, market_avg=90)
    assert not plan.winter
    assert all(s.action != "charge" for s in plan.steps)


def test_unknown_forecast_sets_no_winter_target():
    now = datetime(2026, 1, 14, 20, 0, tzinfo=BXL).astimezone(timezone.utc)
    o = Options()
    plan = make_plan(now, BXL, o, FixedTariff(o), [], _flat_load(0.55), lambda a, b: 0.0, soc=25,
                     forecast_until=now)
    assert not plan.winter
    assert all(s.action != "charge" for s in plan.steps)


def test_dynamic_plan_uses_quarter_hours_and_arbitrage_in_summer():
    o = Options()
    day = datetime(2026, 6, 15, 0, 0, tzinfo=BXL)
    slots = []
    for q in range(96 * 2):
        t = day + timedelta(minutes=15 * q)
        h = t.hour
        price = -10 if 2 <= h < 5 else (260 if 18 <= h < 21 else 110)
        slots.append(PriceSlot(t.astimezone(timezone.utc), (t + timedelta(minutes=15)).astimezone(timezone.utc), price))
    now = datetime(2026, 6, 15, 0, 5, tzinfo=BXL).astimezone(timezone.utc)
    plan = make_plan(now, BXL, o, DynamicTariff(o), slots, _flat_load(0.4), _bell(14.0), soc=20)
    assert not plan.winter
    assert plan.steps[1].hours == 0.25
    hours = {s.start.astimezone(BXL).hour: s.action for s in plan.steps if s.start < now + timedelta(hours=22)}
    assert "charge" in {hours[h] for h in (2, 3, 4)}
    assert "discharge" in {hours[h] for h in (18, 19, 20)}


def test_unreachable_night_target_is_capped_so_no_day_rate_charging_before_the_window():
    """Demo finding: a 100 % target the night window cannot deliver made the plan buy at the day rate at 20:00."""
    now = datetime(2026, 11, 9, 16, 0, tzinfo=BXL).astimezone(timezone.utc)
    o = Options()
    evening = lambda t0, t1: (1.4 if 17 <= t0.astimezone(BXL).hour < 22 else 0.55) * (t1 - t0).total_seconds() / 3600
    plan = make_plan(now, BXL, o, FixedTariff(o), [], evening, _bell(1.5), soc=38, market_avg=90)
    t = plan.targets[0]
    assert t["applied"] and t["capped"] and t["soc"] < 100
    window_start = datetime.fromisoformat(t["window_start"])
    assert all(s.action != "charge" for s in plan.steps if s.start < window_start)
    assert any(s.action == "charge" for s in plan.steps if s.start >= window_start)


def test_a_short_first_slot_is_merged_only_at_the_same_price():
    o = Options()
    fc = lambda a, b: 0.0
    at = lambda h, m: datetime(2026, 11, 10, h, m, tzinfo=BXL).astimezone(timezone.utc)   # a Tuesday
    plan = make_plan(at(22, 55), BXL, o, FixedTariff(o), [], _flat_load(0.5), fc, soc=30, market_avg=90)
    assert plan.steps[0].start == at(22, 55) and plan.steps[0].hours == pytest.approx(65 / 60)   # night + night
    plan = make_plan(at(21, 55), BXL, o, FixedTariff(o), [], _flat_load(0.5), fc, soc=30, market_avg=90)
    assert plan.steps[0].hours == pytest.approx(5 / 60)                                          # day, then night


# ---------------------------------------------------------------- selling before the sun
TOMORROW = datetime(2026, 6, 15, tzinfo=BXL)


def _shaded_forecast(day_kwh, last=20, month=6):
    """Raw sine forecast from 06:00 to `last` on 14-16 June, with trees shading the morning (learned)."""
    raw = {}
    for d in (14, 15, 16):
        for h in range(6, last):
            raw[datetime(2026, month, d, h, tzinfo=BXL).astimezone(timezone.utc)] = math.sin(math.pi * (h + 0.5 - 6) / (last - 6))
    scale = day_kwh / sum(v for t, v in raw.items() if t.astimezone(BXL).day == 15)
    shade = ShadeModel()
    for h in range(6, 11):
        for _ in range(60):
            shade.learn(month, h, 0.25, 1.0)
    return PvForecast({t: v * scale for t, v in raw.items()}, shade, BXL)


def _prices(fn, minutes=60, start=datetime(2026, 6, 14, tzinfo=BXL)):
    out = []
    for k in range(72 * 60 // minutes):
        t = start + timedelta(minutes=minutes * k)
        out.append(PriceSlot(t.astimezone(timezone.utc), (t + timedelta(minutes=minutes)).astimezone(timezone.utc), fn(t)))
    return out


def _day(evening, morning=150, midday=20, other=95):
    """Hourly market prices (EUR/MWh): evening 18-22 h, morning 6-9 h, cheap midday sun 11-17 h."""
    return lambda t: evening if 18 <= t.hour < 22 else morning if 6 <= t.hour < 9 else midday if 11 <= t.hour < 17 else other


def _sunny_plan(day_kwh=35, evening=210, last=20, prices=None, minutes=60, **opts):
    """17:00 with the battery at 95 % and a sunny day ahead; returns (options, forecast, plan, sunrise index).
    The sunrise is the first step tomorrow where the shade-corrected solar covers the house."""
    o = Options(**opts)
    pv = _shaded_forecast(day_kwh, last)
    now = datetime(2026, 6, 14, 17, 0, tzinfo=BXL).astimezone(timezone.utc)
    plan = make_plan(now, BXL, o, DynamicTariff(o), _prices(prices or _day(evening), minutes), _flat_load(0.45),
                     pv.kwh, soc=95)
    sun = next(i for i, s in enumerate(plan.steps) if s.start >= TOMORROW and s.pv_kwh >= s.load_kwh)
    return o, pv, plan, sun


@pytest.mark.parametrize("case", ["sunny", "high reserve", "evening beats the night price"])
def test_battery_is_sold_down_before_the_shaded_sunrise(case):
    """Tomorrow's sun fills the battery anyway: what the house does not need until the sun covers
    it (shade-corrected) is sold beforehand, at the best prices, as far as the settings allow."""
    opts = {"battery_reserve_soc": 60} if case == "high reserve" else {}
    o, pv, plan, i = _sunny_plan(evening=330 if case == "evening beats the night price" else 210, **opts)
    steps, sun = plan.steps, plan.steps[i]
    before = steps[:i]
    raw_sun = next(s for s in steps if s.start >= TOMORROW
                   and pv.kwh(s.start, s.start + timedelta(hours=s.hours), corrected=False) >= s.load_kwh)
    assert sun.start > raw_sun.start                          # the trees delay the moment the sun takes over
    sold = [s for s in before if s.action == "discharge"]
    assert sold and {s.sell for s in sold} == {max(s.sell for s in before)}   # only in the best-paid hours
    assert min(s.soc_end for s in sold) >= o.battery_reserve_soc - 1e-9       # never below the reserve
    assert all(s.reserve is None for s in before)                             # (no extreme prices tonight)
    assert max(s.soc_end for s in steps[i:] if s.start < TOMORROW + timedelta(hours=17)) >= 99   # sun refills it
    bought = sum(s.import_kwh for s in before)
    if case == "sunny":
        assert sun.soc_start <= o.battery_min_soc + 2                        # nothing left that the sun replaces
        assert bought < 0.25                                                 # the house was covered until then
    elif case == "high reserve":
        b = Battery.from_options(o)
        last = max(j for j, s in enumerate(before) if s.action == "discharge")
        house = sum(max(s.load_kwh - s.pv_kwh, 0) / b.eta_d for s in steps[last + 1:i]) / (b.capacity_kwh / 100)
        assert min(s.soc_end for s in sold) == pytest.approx(60, abs=1)       # sold down to the reserve...
        assert sun.soc_start == pytest.approx(max(o.battery_min_soc, 60 - house), abs=3)   # ...the house used the rest
        assert bought < 0.25
    else:
        b = Battery.from_options(o)
        night_buy = max(s.buy for s in before if s.start.astimezone(BXL).hour < 6)
        assert max(s.sell for s in before) * b.eta_d - b.wear_c_kwh > night_buy   # selling beats buying back
        assert min(s.soc_end for s in sold) == pytest.approx(o.battery_reserve_soc, abs=1)
        assert bought > 0.5                                  # so the house buys its night energy instead


@pytest.mark.parametrize("day_kwh", [12, 25])
def test_battery_is_kept_when_selling_early_does_not_pay(day_kwh):
    """12 kWh: the sun will not refill it. 25 kWh with sun until 20 h: the battery only fills late,
    and the solar it would push out sells tomorrow evening at the same price, without battery losses."""
    o, _, plan, i = _sunny_plan(day_kwh=day_kwh)
    sun = plan.steps[i]
    if day_kwh == 12:
        assert all(s.action != "discharge" for s in plan.steps[:i])
        assert sun.soc_start > 50
    else:
        assert sun.soc_start > o.battery_min_soc + 5
        evening = [s for s in plan.steps if TOMORROW + timedelta(hours=18) <= s.start < TOMORROW + timedelta(hours=20)]
        assert sum(s.export_kwh for s in evening) > 0.2


def _evening_peak(t):
    return {18: 150, 19: 150, 20: 300, 21: 150}.get(t.hour, 20 if 11 <= t.hour < 17 else 95)


def _half_hour_peak(t):
    h = t.hour + t.minute / 60
    return (300 if 20.5 <= h < 21 else 150) if 18 <= h < 22 else 20 if 11 <= h < 17 else 95


def _morning_peak(t):
    if t.day == 15 and t.hour in (7, 8):
        return 320
    return 150 if 18 <= t.hour < 22 else 20 if 11 <= t.hour < 17 else 95


@pytest.mark.parametrize("prices,minutes,best", [(_evening_peak, 60, [(14, 20)]),
                                                 (_half_hour_peak, 15, [(14, 20.5), (14, 20.75)]),
                                                 (_morning_peak, 60, [(15, 7), (15, 8)])])
def test_export_waits_for_the_best_prices(prices, minutes, best):
    """Not everything at the first good price: the best-paid slots get full power first."""
    o, _, plan, i = _sunny_plan(day_kwh=25, prices=prices, minutes=minutes)
    b = Battery.from_options(o)
    full = int(b.max_discharge_kw * minutes / 60 / (b.capacity_kwh / 100)) - 1
    at = {(s.start.astimezone(BXL).day, s.start.astimezone(BXL).hour + s.start.astimezone(BXL).minute / 60): s
          for s in plan.steps}
    top = [at[k] for k in best]
    for s in top:
        assert s.action == "discharge" and s.soc_start - s.soc_end >= full, s
    others = [s for s in plan.steps[:i] if s.action == "discharge" and s not in top]
    assert all(s.sell < top[0].sell for s in others)
    if prices is _morning_peak:                          # kept overnight for the better morning price
        assert not any(s.action == "discharge" for s in plan.steps if s.start < TOMORROW)


@pytest.mark.parametrize("limit,early", [(6.0, False), (2.5, True)])
def test_fixed_sells_early_only_when_tomorrows_surplus_would_be_curtailed(limit, early):
    """A fixed contract pays the same for export at every hour, so selling early only loses
    to the battery; unless tomorrow's surplus is more than the export limit lets out."""
    o = Options(export_limit_kw=limit)
    now = datetime(2026, 6, 14, 17, 0, tzinfo=BXL).astimezone(timezone.utc)
    plan = make_plan(now, BXL, o, FixedTariff(o), [], _flat_load(0.45), _shaded_forecast(40).kwh, soc=95, market_avg=90)
    sun = next(s.start for s in plan.steps if s.start >= TOMORROW and s.pv_kwh >= s.load_kwh + 2.0)
    assert any(s.action == "discharge" and s.start < sun for s in plan.steps) is early


# ---------------------------------------------------------------- buying to sell later
def _arbitrage(evening_market):
    """Midnight, battery at the reserve, no house load: 9 cheap night hours and an evening price;
    nothing else makes stored energy worth anything (no value for energy left at the end)."""
    dyn = DynamicTariff(Options())
    market = [0] * 9 + [120] * 9 + [evening_market] * 4 + [120] * 2
    buy = [dyn.buy(None, m) for m in market]
    sell = [dyn.sell(None, m) for m in market]
    return optimize(B, hourly(buy, sell, load=0.0), soc0=B.reserve_soc, terminal_c_kwh=0)


def _break_even_market():
    """Evening market price at which a kWh bought at the cheap price and sold in the evening breaks
    even after charging and discharging losses and battery wear."""
    dyn = DynamicTariff(Options())
    per_dc_kwh = dyn.buy(None, 0) / B.eta_c + B.wear_c_kwh          # bought, stored, and worn
    sell = per_dc_kwh / B.eta_d                                        # what the evening must pay per kWh sold
    return (sell - dyn.sell(None, 0)) / Options().dynamic_sell_mult


@pytest.mark.parametrize("factor", [1.3, 0.8])
def test_battery_is_filled_completely_when_selling_it_later_pays(factor):
    steps = _arbitrage(_break_even_market() * factor)
    if factor > 1:
        assert max(s.soc_end for s in steps[:9]) >= 99                  # full before the evening
        assert all(s.import_kwh / s.hours <= B.import_limit_kw for s in steps)   # no extra capacity peak for it
        assert sum(s.export_kwh for s in steps[18:22]) > 0.95 * B.kwh(100 - B.reserve_soc) * B.eta_d   # sold
        assert steps[21].soc_end <= B.reserve_soc + 1
    else:
        assert all(s.action != "charge" for s in steps)                 # losses and wear eat the spread


@pytest.mark.parametrize("base,evening,pays", [(0, 250, True), (100, 130, False)])
def test_battery_is_charged_only_for_what_the_house_uses_in_the_dear_hours(base, evening, pays):
    """Exporting earns nothing here, so stored energy is only worth what it saves the house. It is
    bought cheaply just before the dear evening, as much as the house uses then and no more, when
    the price difference beats losses and wear."""
    dyn = DynamicTariff(Options())
    market = [base] * 17 + [evening] * 5 + [base] * 2
    steps = optimize(B, hourly([dyn.buy(None, m) for m in market], [-1.0] * 24, load=0.5), soc0=B.min_soc,
                     terminal_c_kwh=0)
    dear = steps[17:22]
    assert all(s.action != "discharge" for s in steps)                   # nothing is sold
    if not pays:                                    # 23.8 c now against 27.1 c later: losses and wear eat it
        assert all(s.action != "charge" for s in steps)
        return
    need = sum(s.load_kwh for s in dear) / B.eta_d / (B.capacity_kwh / 100)   # SOC points the house uses
    assert any(s.action == "charge" for s in steps[:17]) and all(s.action != "charge" for s in dear)
    assert dear[0].soc_start == pytest.approx(B.min_soc + need, abs=3)   # what the evening needs...
    assert max(s.soc_end for s in steps) <= B.min_soc + need + 3         # ...and no more (it is not filled up)
    assert dear[-1].soc_end <= B.min_soc + 1                             # used up by the end of the dear hours
    assert sum(s.import_kwh for s in dear) < 0.25                        # the house hardly buys at the dear price


# ---------------------------------------------------------------- price spikes and the capacity tariff
MARCH = datetime(2026, 3, 14, tzinfo=BXL)


def _spike_plan(prices, soc=30, at=10, month_peak_kw=0.0, **opts):
    """A dull March day (winter: no export normally), no solar; market prices from `prices(local time)`."""
    o = Options(**opts)
    now = datetime(2026, 3, 14, at, 0, tzinfo=BXL).astimezone(timezone.utc)
    return o, make_plan(now, BXL, o, DynamicTariff(o), _prices(prices, start=MARCH), _flat_load(0.45),
                        lambda a, z: 0.0, soc=soc, month_peak_kw=month_peak_kw)


def _hour(plan, h):
    return next(s for s in plan.steps if s.start.astimezone(BXL).hour == h and s.start.astimezone(BXL).day == 14)


def _full_power_kw(o, house=0.45):
    b = Battery.from_options(o)
    return house + b.max_charge_kw / b.eta_c


def test_negative_price_charges_at_full_power_when_the_month_peak_is_already_higher():
    """This month already has a 3.6 kW quarter-hour: charging up to it costs nothing extra."""
    o, plan = _spike_plan(lambda t: -200 if (t.day == 14 and t.hour in (12, 13)) else 100, month_peak_kw=3.6)
    for h in (12, 13):
        assert _hour(plan, h).import_kwh == pytest.approx(_full_power_kw(o), abs=0.05)
    assert plan.boost and plan.boost["peak_cost_eur"] == 0
    assert max(s.import_kwh / s.hours for s in plan.steps) <= 3.6


def test_negative_price_stays_under_the_free_peak_when_crossing_costs_more():
    """-200 EUR/MWh pays about 0.2 EUR/kWh: not enough for a new 3.5 kW peak (about 4 EUR)."""
    o, plan = _spike_plan(lambda t: -200 if (t.day == 14 and t.hour in (12, 13)) else 100, month_peak_kw=1.8)
    assert plan.boost is None
    assert _hour(plan, 12).action == "charge"
    assert max(s.import_kwh / s.hours for s in plan.steps) <= o.capacity_min_kw + 1e-9


def test_extreme_negative_price_charges_as_fast_as_possible_above_the_limit():
    o, plan = _spike_plan(lambda t: -8000 if (t.day == 14 and t.hour == 13) else 100)
    assert _hour(plan, 13).import_kwh == pytest.approx(_full_power_kw(o), abs=0.05)
    assert plan.boost and plan.boost["gain_eur"] > plan.boost["peak_cost_eur"] > 0


def test_the_higher_peak_is_paid_once_however_often_it_is_used():
    o, plan = _spike_plan(lambda t: -8000 if (t.day == 14 and t.hour in (11, 15)) else 100, soc=15)
    for h in (11, 15):
        assert _hour(plan, h).import_kwh > o.import_limit_kw + 0.5
    once = (plan.boost["limit_kw"] - o.capacity_min_kw) * o.capacity_tariff_eur_kw_year / 12
    assert plan.boost["peak_cost_eur"] == pytest.approx(once, abs=0.01)


def test_before_an_extreme_price_the_battery_is_charged_as_fast_as_possible_and_sold_below_the_reserve():
    o, plan = _spike_plan(lambda t: 8000 if (t.day == 14 and t.hour in (12, 13)) else 100, soc=12, at=11)
    assert plan.winter                                                   # export is allowed anyway
    assert _hour(plan, 11).import_kwh == pytest.approx(_full_power_kw(o), abs=0.05)
    spike = [_hour(plan, h) for h in (12, 13)]                           # (either hour pays the same)
    assert sum(s.import_kwh for s in spike) == 0                         # the house is not bought for at 8000
    assert sum(s.export_kwh for s in spike) > 1.5                        # the rest is sold
    assert spike[-1].soc_end == pytest.approx(o.battery_min_soc, abs=1)  # sold below the 35 % reserve
    assert any(s.reserve == o.battery_min_soc for s in spike)            # and the step tells the controller


def test_without_a_capacity_tariff_the_import_limit_is_never_crossed():
    """Then the import limit is the user's own limit (e.g. a fuse), not a cost."""
    o, plan = _spike_plan(lambda t: 8000 if (t.day == 14 and t.hour in (12, 13)) else 100, soc=12, at=11,
                          capacity_tariff_eur_kw_year=0)
    assert plan.boost is None
    assert all(s.import_kwh / s.hours <= o.import_limit_kw for s in plan.steps)


def test_winter_exports_only_when_buying_back_later_costs_less():
    """300 EUR/MWh in the evening is not enough on a dull day: the house would buy it back at
    least as dear. An extreme price is."""
    _, plan = _spike_plan(lambda t: 300 if 18 <= t.hour < 22 else 150 if 6 <= t.hour < 9 else 95, soc=90)
    assert plan.winter and all(s.action != "discharge" for s in plan.steps)
    _, plan = _spike_plan(lambda t: 4000 if (t.day == 14 and t.hour == 19) else 95, soc=90)
    assert plan.winter and _hour(plan, 19).action == "discharge"


# ---------------------------------------------------------------- not switching back and forth
def _two_good_hours(first_sell=30.0):
    """Two equally good hours to sell in, and only enough battery above the reserve for one."""
    buy = [25.0] * 6
    sell = [first_sell, 30.0, 5.0, 5.0, 5.0, 5.0]
    return hourly(buy, sell, load=0.0)


@pytest.mark.parametrize("now,first", [("self_use", "self_use"), ("discharge", "discharge")])
def test_an_indifferent_choice_keeps_doing_what_is_being_done(now, first):
    steps = optimize(B, _two_good_hours(), soc0=B.reserve_soc + 25, terminal_c_kwh=0, first_action=now)
    assert steps[0].action == first
    assert any(s.action == "discharge" for s in steps[:2])            # it sells in one of the two either way


def test_a_clearly_better_choice_still_switches():
    steps = optimize(B, _two_good_hours(first_sell=45.0), soc0=B.reserve_soc + 25, terminal_c_kwh=0,
                     first_action="self_use")
    assert steps[0].action == "discharge"


def test_a_finished_charge_is_not_sticky_so_nothing_charges_past_its_target():
    from pilot.planner import Plan, Step, sticky_action
    t = T0
    plan = Plan([Step(t, 1.0, "charge", 40, 60, 0, 0, 0, 0, 10, 5, 0)], t, "fixed")
    assert sticky_action(plan, t + timedelta(minutes=30), 55, 35) == "charge"
    assert sticky_action(plan, t + timedelta(minutes=30), 60, 35) is None       # target reached
    plan = Plan([Step(t, 1.0, "discharge", 60, 20, 0, 0, 0, 0, 10, 30, 0)], t, "dynamic")
    assert sticky_action(plan, t, 50, 35) == "discharge"
    assert sticky_action(plan, t, 35, 35) is None                               # at the reserve: done
    plan.steps[0].reserve = 10
    assert sticky_action(plan, t, 35, 35) == "discharge"                        # an extreme step goes lower
    assert sticky_action(None, t, 50, 35) is None
