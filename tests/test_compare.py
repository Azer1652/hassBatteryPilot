"""The nightly replay of one day per contract (compare.replay_day)."""
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from pilot.compare import Comparison, replay_day
from pilot.config import Options
from pilot.load import LoadProfile
from pilot.meter import QuarterRecord
from pilot.prices import PriceSlot
from pilot.tariffs import DynamicTariff, FixedTariff

BXL = ZoneInfo("Europe/Brussels")
DAY = datetime(2026, 6, 14, tzinfo=BXL)       # a sunny Sunday
# EUR/MWh per local hour: deeply negative around noon, an evening peak
MARKET = [85, 82, 80, 78, 80, 85, 95, 100, 60, 10, -5, -30, -60, -80, -60, -30, 5, 40, 90, 130, 180, 210, 160, 110]


def pv_kw(t):
    h = t.astimezone(BXL).hour + t.astimezone(BXL).minute / 60 + 7.5 / 60
    return 40 * math.pi / 30 * math.sin(math.pi * (h - 6) / 15) if 6 <= h < 21 else 0.0   # 40 kWh a day


def sunny_day():
    prices, recs = [], []
    for d in range(2):
        for h, p in enumerate(MARKET):
            t = (DAY + timedelta(days=d, hours=h)).astimezone(timezone.utc)
            prices.append(PriceSlot(t, t + timedelta(hours=1), p))
    for q in range(96):
        t = (DAY + timedelta(minutes=15 * q)).astimezone(timezone.utc)
        load, pv = 0.4 / 4, pv_kw(t) / 4
        recs.append(QuarterRecord(t, {"import": 0.0, "export": 0.0, "load": load, "pv": pv},
                                  {"import": 1.0, "export": 1.0, "load": 1.0, "pv": 1.0}, None))

    def forecast(t0, t1):
        return sum(r.kwh["pv"] for r in recs if t0 <= r.start < t1)
    return recs, prices, forecast


def replay(kind, block):
    recs, prices, fc = sunny_day()
    return replay_day(recs, kind, Options(), BXL, prices, LoadProfile(default_kw=0.4), fc, 90.0,
                      market_avg=60.0, block_negative=block)


def negative_export(res):
    """kWh exported in quarters where the dynamic sell price is below zero."""
    tar = DynamicTariff(Options())
    _, prices, _ = sunny_day()
    out = 0.0
    for t, _imp, exp in res["flows"]:
        market = next(p.eur_mwh for p in prices if p.start <= t < p.end)
        if tar.sell(t.astimezone(BXL), market) < 0:
            out += exp
    return out


def test_dynamic_replay_pays_for_export_at_negative_prices_unless_blocked():
    free, blocked = replay("dynamic", False), replay("dynamic", True)
    # The battery takes at most 2.56 kW, so on a 40 kWh day solar still spills at noon.
    assert negative_export(free) > 2
    assert negative_export(blocked) == pytest.approx(0, abs=1e-9)
    assert blocked["cost_c"] < free["cost_c"] - 20          # the negative hours no longer cost money
    assert blocked["exp"] > 5                                # positive-price hours are still sold


def test_fixed_replay_is_the_same_with_blocking():
    # The fixed sell price follows the month's average (here positive), so nothing is blocked.
    free, blocked = replay("fixed", False), replay("fixed", True)
    assert blocked["cost_c"] == pytest.approx(free["cost_c"])
    assert blocked["exp"] == pytest.approx(free["exp"])


def test_a_gap_in_the_prices_is_priced_like_the_day_before_not_at_zero():
    """A price sensor that misses half a day must not make dynamic look cheap (it priced the gap at 0)."""
    recs, prices, fc = sunny_day()
    yesterday = [PriceSlot(p.start - timedelta(days=1), p.end - timedelta(days=1), p.eur_mwh) for p in prices[:24]]
    full = yesterday + prices
    gap = ((DAY + timedelta(hours=12)).astimezone(timezone.utc), (DAY + timedelta(days=1)).astimezone(timezone.utc))
    gappy = [p for p in full if not gap[0] <= p.start < gap[1]]
    args = (Options(), BXL)
    a = replay_day(recs, "dynamic", *args, full, LoadProfile(default_kw=0.4), fc, 50.0, market_avg=60.0)
    b = replay_day(recs, "dynamic", *args, gappy, LoadProfile(default_kw=0.4), fc, 50.0, market_avg=60.0)
    assert b["cost_c"] == pytest.approx(a["cost_c"], rel=0.1, abs=20)



def test_a_blocked_quarter_is_replayed_with_what_the_panels_could_give():
    """Export was blocked from 10:00 to 16:00 and the inverter held the panels back to the house load.
    The fixed contract would not have blocked: its replay must not lose that solar."""
    recs, prices, fc = sunny_day()
    held = []
    for r in recs:
        blocked = 10 <= r.start.astimezone(BXL).hour < 16
        held.append(QuarterRecord(r.start, {**r.kwh, "pv": r.kwh["load"] if blocked else r.kwh["pv"]}, r.covered,
                                  blocked=blocked))
    args = (Options(), BXL, prices, LoadProfile(default_kw=0.4), fc, 90.0)
    real = replay_day(recs, "fixed", *args, market_avg=60.0)
    marked = replay_day(held, "fixed", *args, market_avg=60.0)
    assert marked["exp"] == pytest.approx(real["exp"]) and marked["cost_c"] == pytest.approx(real["cost_c"])
    for r in held:
        r.blocked = False                                 # (not marked: the sun seems to have gone)
    assert replay_day(held, "fixed", *args, market_avg=60.0)["exp"] < real["exp"] - 10


def _quarter(hour, minute=0):
    t = (DAY + timedelta(hours=hour, minutes=minute)).astimezone(timezone.utc)
    return QuarterRecord(t, {"import": 0.25, "export": 0.1, "load": 0.25, "pv": 0.1},
                         {"import": 1.0, "export": 1.0, "load": 1.0, "pv": 1.0})


def test_same_usage_prices_both_contracts_on_the_quarters_that_have_a_price():
    """A price sensor added during the month: the hours before it counted for fixed only, so
    dynamic looked cheaper by all of them."""
    o = Options()
    both, late = Comparison(), Comparison()
    for h in range(24):
        both.add_quarter(_quarter(h), 100.0 if h >= 12 else None, o, BXL)
        if h >= 12:
            late.add_quarter(_quarter(h), 100.0, o, BXL)
    a, b = both.report("2026-06", o), late.report("2026-06", o)
    assert a["bought_kwh"] == 6.0 and b["bought_kwh"] == 3.0          # the meter still counts every hour
    assert a["same_usage"] == b["same_usage"]
    none = Comparison()
    for h in range(24):
        none.add_quarter(_quarter(h), None, o, BXL)
    r = none.report("2026-06", o)                                     # no prices at all: the fixed bill only
    assert r["same_usage"]["dynamic_eur"] is None
    energy = 6.0 * (o.fixed_night_rate + o.grid_fees_c_kwh) / 100 - 2.4 * FixedTariff(o).sell(None, None, 0.0) / 100
    share = 24 / (30 * 96)                    # 24 quarter-hours of June: fees and capacity tariff for that part
    assert r["same_usage"]["fixed_eur"] == pytest.approx(
        energy + (o.fixed_yearly_fee_eur / 12 + 2.5 * 52.37 / 12) * share, abs=0.01)


def test_fees_count_for_the_part_of_the_month_that_is_in_the_bill():
    """A few priced hours against a whole month of fees: the contract with the lower yearly fee
    looked cheaper by that fee (4 EUR a month) although its energy cost more."""
    o = Options(fixed_yearly_fee_eur=65, dynamic_yearly_fee_eur=15)
    c = Comparison()
    for h in range(8):                                             # one evening with prices, market at 350 EUR/MWh
        c.add_quarter(_quarter(h), 350.0, o, BXL)
    r = c.report("2026-06", o)
    assert r["days"] == pytest.approx(8 / 96, abs=0.05) and r["month_days"] == 30
    assert r["same_usage"]["dynamic_eur"] > r["same_usage"]["fixed_eur"]      # the dearer energy decides
    assert r["same_usage"]["fixed_eur"] < 1.0                                  # not a month of fees
    full = Comparison()
    m = full.month("2026-06")
    m.update(quarters=30 * 96, market_n=30 * 96, market_sum=100.0 * 30 * 96)
    whole = full.report("2026-06", o)                                         # a whole month: the whole fees
    assert whole["days"] == 30
    assert whole["same_usage"]["fixed_eur"] == pytest.approx(65 / 12 + 2.5 * 52.37 / 12, abs=0.01)
    assert whole["same_usage"]["dynamic_eur"] == pytest.approx(15 / 12 + 2.5 * 52.37 / 12, abs=0.01)


def test_replayed_days_carry_their_part_of_the_fees():
    o = Options()
    c = Comparison()
    key = "2026-06"
    for kind in ("fixed", "dynamic"):
        c.add_replay(key, kind, {"cost_c": 300.0, "imp": 10.0, "exp": 0.0, "charged": 0.0, "discharged": 0.0,
                                 "grid_charged": 0.0, "peak_kw": 2.0})
    c.month(key)["replay_days"] = 3
    ws = c.report(key, o)["with_strategy"]
    part = 3 / 30
    assert ws["fixed"]["eur"] == pytest.approx(3.0 + (o.fixed_yearly_fee_eur / 12 + 2.5 * 52.37 / 12) * part, abs=0.01)
    assert ws["dynamic"]["eur"] == pytest.approx(3.0 + (o.dynamic_yearly_fee_eur / 12 + 2.5 * 52.37 / 12) * part, abs=0.01)


def test_a_month_saved_before_priced_quarters_were_kept_apart_is_unchanged():
    o = Options()
    c = Comparison()
    for h in range(24):
        c.add_quarter(_quarter(h), 100.0, o, BXL)
    before = c.report("2026-06", o)
    old = Comparison.from_json({"months": {"2026-06": {k: v for k, v in c.months["2026-06"].items()
                                                       if not k.startswith("p_")}}})
    assert old.report("2026-06", o)["same_usage"] == before["same_usage"]


def test_the_energy_left_in_the_battery_is_credited_to_the_day():
    """A day that ends with a fuller battery than it started is not charged for what the next day uses."""
    recs, prices, fc = sunny_day()
    o = Options()
    res = {s: replay_day(recs, "fixed", o, BXL, prices, LoadProfile(default_kw=0.4), fc, s, market_avg=60.0)
           for s in (20.0, 90.0)}
    for soc0, r in res.items():
        stored_kwh = (r["soc_end"] - soc0) * o.battery_capacity_kwh / 100
        if abs(stored_kwh) > 0.1:
            assert r["stored_c"] * stored_kwh < 0               # fuller: a credit, emptier: a cost
        else:
            assert r["stored_c"] == pytest.approx(0.0, abs=5)
    # A battery that starts low fills from the sun: the day earns the energy it leaves behind.
    assert res[20.0]["soc_end"] > 20.0 and res[20.0]["stored_c"] < 0


def test_the_stored_change_is_kept_apart_in_the_report():
    o = Options()
    c = Comparison()
    for kind in ("fixed", "dynamic"):
        c.add_replay("2026-06", kind, {"cost_c": 300.0, "imp": 10.0, "exp": 0.0, "charged": 0.0, "discharged": 0.0,
                                       "grid_charged": 0.0, "stored_c": -120.0, "peak_kw": 2.0})
    c.month("2026-06")["replay_days"] = 1
    assert c.report("2026-06", o)["with_strategy"]["fixed"]["stored_eur"] == -1.2
