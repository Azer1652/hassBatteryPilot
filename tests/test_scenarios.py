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

from pilot.config import Options  # noqa: E402

LIMIT = Options().import_limit_kw


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


def test_winter_fixed_charges_at_night_and_keeps_peaks_low(winter):
    _, m = winter
    assert m["fixed"]["grid_charged"] > 7 * 4          # several kWh every winter night
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
    _, m = week("2026-03-09", price_scale=scale)
    o = Options()
    f, d = yearsim.month_eur(m, "fixed", o), yearsim.month_eur(m, "dynamic", o)
    assert (d < f) if cheaper == "dynamic" else (f < d)


def test_peaks_only_winter_mode_is_cheaper_and_still_shaves(winter):
    _, full = winter
    _, peaks = week("2026-01-12", opts=Options(winter_mode="peaks_only"))
    o = Options()
    assert peaks["fixed"]["grid_charged"] < full["fixed"]["grid_charged"] / 2
    assert peaks["fixed"]["peak_kw"] <= LIMIT + 0.1
    assert yearsim.month_eur(peaks, "fixed", o) < yearsim.month_eur(full, "fixed", o)
