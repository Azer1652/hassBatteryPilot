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
