"""Whole weeks of a synthetic house (tools/yearsim.py), replayed the way the app does every night.

Checks the behaviour that matters: winter night charging, no grid charging on sunny
summer days, evening export on dynamic prices, peaks kept under the limit, tree shade
learned, and a comparison that follows the prices.
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import yearsim  # noqa: E402
from fake_ha import RATES_EARLY_2026  # noqa: E402

from pilot.config import Options  # noqa: E402

LIMIT = Options().import_limit_kw
pytestmark = pytest.mark.slow       # whole simulated weeks: left out of `python tools/check.py --fast`


def week(start, **kw):
    res = yearsim.run(date.fromisoformat(start), 7, replan_every_h=1, **kw)   # the app replans every 5 min
    (m,) = res["months"].values()
    return res, m


@pytest.fixture(scope="module")
def winter():
    return week("2026-01-12")


@pytest.fixture(scope="module")
def summer():
    return week("2026-07-06")


CHEAP_NIGHT = Options(fixed_night_rate=8.0)      # a night rate low enough to pay for losses and wear


def test_winter_fixed_charges_at_night_when_it_pays_and_keeps_peaks_low(winter):
    _, m = winter
    _, cheap = week("2026-01-12", opts=CHEAP_NIGHT)
    assert cheap["fixed"]["grid_charged"] > 7 * 4      # several kWh every winter night
    assert cheap["fixed"]["peak_kw"] <= LIMIT + 0.05
    # The default rates: the night is 3.4 c cheaper, less than losses and wear. No night filling.
    assert m["fixed"]["grid_charged"] < 7 * 2.5        # only what the peaks need
    assert m["fixed"]["peak_kw"] <= LIMIT + 0.05
    assert m["dynamic"]["peak_kw"] <= LIMIT + 0.05
    assert m["nobat"]["peak_kw"] > 3                    # without the battery the cooking spike hits the grid


def test_winter_learns_the_tree_shade(winter):
    res, _ = winter
    sh = res["shade"]
    assert sh.factor(1, 10) < 0.5 and sh.factor(1, 15) < 0.5     # low sun behind the trees (true 0.3)
    assert sh.factor(1, 12) > 0.75                               # midday clear of them (true 0.9)


def test_summer_fixed_does_not_buy_for_the_battery(summer):
    _, m = summer
    assert m["fixed"]["grid_charged"] < 3                 # the sun fills it
    assert m["fixed"]["exp"] > 20                         # surplus solar is sold


def test_summer_dynamic_exports_in_the_evening_and_is_cheaper(summer):
    _, m = summer
    o = Options()
    assert m["dynamic"]["discharged"] > m["fixed"]["discharged"]
    assert yearsim.month_eur(m, "dynamic", o) < yearsim.month_eur(m, "fixed", o)


def test_battery_beats_no_battery_in_summer(summer):
    _, m = summer
    o = Options()
    for kind in ("fixed", "dynamic"):
        assert yearsim.month_eur(m, kind, o) < yearsim.month_eur(m, kind, o, battery=False)


@pytest.mark.parametrize("scale,cheaper", [(0.4, "dynamic"), (2.5, "fixed")])
def test_comparison_follows_the_market(scale, cheaper):
    o = Options(**RATES_EARLY_2026)     # (on the rates of late 2026 dynamic wins this week at any market level)
    _, m = week("2026-03-09", price_scale=scale, opts=o)
    f, d = yearsim.month_eur(m, "fixed", o), yearsim.month_eur(m, "dynamic", o)
    assert (d < f) if cheaper == "dynamic" else (f < d)


def test_peaks_only_winter_mode_never_fills_at_night_and_still_shaves():
    _, full = week("2026-01-12", opts=CHEAP_NIGHT)
    _, peaks = week("2026-01-12", opts=Options(winter_mode="peaks_only", fixed_night_rate=8.0))
    assert peaks["dynamic"]["grid_charged"] < full["dynamic"]["grid_charged"] + 1
    assert peaks["fixed"]["peak_kw"] <= LIMIT + 0.1
    assert peaks["dynamic"]["peak_kw"] <= LIMIT + 0.1


def test_peak_shaving_saves_in_winter_and_changes_nothing_in_summer(winter, summer):
    """With the inverter's peak-shaving switch the battery is kept for the peaks instead of being
    emptied by the house and topped up again."""
    o = Options()
    _, m = winter
    _, shave = week("2026-01-12", peak_shave=True)
    assert shave["fixed"]["grid_charged"] < m["fixed"]["grid_charged"] / 2
    assert shave["fixed"]["peak_kw"] <= LIMIT + 0.05 and shave["dynamic"]["peak_kw"] <= LIMIT + 0.05
    for kind in ("fixed", "dynamic"):
        assert yearsim.month_eur(shave, kind, o) < yearsim.month_eur(m, kind, o)
    _, s = summer
    _, s_shave = week("2026-07-06", peak_shave=True)
    for kind in ("fixed", "dynamic"):
        assert yearsim.month_eur(s_shave, kind, o) <= yearsim.month_eur(s, kind, o) + 0.01


def test_price_spikes_make_dynamic_cheaper_and_never_dearer(monkeypatch):
    """Four synthetic March days with an extreme negative and an extreme positive hour, replayed
    with the rules that cross the import limit and the reserve, and with those rules switched off."""
    import pilot.planner as planner

    spikes = {date(2026, 3, 10): {3: -8000.0}, date(2026, 3, 11): {19: 3000.0}}
    real = yearsim.World.prices

    def prices(self, day):
        out = real(self, day)
        for h, p in spikes.get(day, {}).items():
            out[h] = type(out[h])(out[h].start, out[h].end, p)
        return out

    monkeypatch.setattr(yearsim.World, "prices", prices)
    o = Options(contract="dynamic")

    def cost():
        (m,) = yearsim.run(date(2026, 3, 9), 4, o, warmup_days=2, replan_every_h=2)["months"].values()
        return yearsim.month_eur(m, "dynamic", o)

    with_rules = cost()
    monkeypatch.setattr(planner, "BOOST_MIN_GAIN_C", 1e9)
    monkeypatch.setattr(planner, "EXTREME_MARGIN_C", 1e9)
    without = cost()
    assert with_rules < without - 0.25                    # incl. the capacity cost of any higher peak
