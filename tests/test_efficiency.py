"""The measured round-trip efficiency (display only)."""
from datetime import datetime, timedelta, timezone

import pytest

from pilot.efficiency import NEEDED_POINTS, EfficiencyMeter
from pilot.meter import CHANNELS, QuarterRecord

T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
CAP = 16.0


def day(d, eta=0.85 ** 0.5, covered=1.0, lo=30.0, grid=False):
    """Charge 40 points from solar (or the grid) and give them back to the house: whole SOC points only."""
    recs, soc, t = [], lo, T0 + timedelta(days=d)
    for i in range(96):
        kwh = {c: 0.0 for c in CHANNELS}
        kwh["load"] = 0.1
        if 20 <= i < 40:                                   # 2 points a quarter in
            dc = 2 * CAP / 100
            kwh["import" if grid else "pv"] = 0.1 + dc / eta
            nxt = soc + 2
        elif 50 <= i < 90:                                 # 1 point a quarter out
            kwh["load"] = CAP / 100 * eta
            nxt = soc - 1
        else:
            kwh["import"], nxt = 0.1, soc
        recs.append(QuarterRecord(t + timedelta(minutes=15 * i), kwh, {c: covered for c in CHANNELS}, soc))
        soc = nxt
    return recs


def test_a_known_round_trip_is_measured():
    m = EfficiencyMeter()
    for d in range(10):
        m.learn_records(day(d))
    r = m.report(90)
    assert r["round_trip_pct"] == pytest.approx(85, abs=1)
    assert r["solar_round_trip_pct"] == pytest.approx(85, abs=1) and r["grid_round_trip_pct"] is None
    assert r["configured_pct"] == 90 and r["charge_points"] >= NEEDED_POINTS
    m2 = EfficiencyMeter.from_json(m.to_json())
    assert m2.report(90)["round_trip_pct"] == r["round_trip_pct"]


def test_grid_and_solar_charging_are_kept_apart():
    m = EfficiencyMeter()
    for d in range(10):
        m.learn_records(day(d, eta=0.8 ** 0.5, grid=True))
    r = m.report(90)
    assert r["grid_round_trip_pct"] == pytest.approx(80, abs=1) and r["solar_round_trip_pct"] is None


def test_no_value_before_there_is_enough_data():
    m = EfficiencyMeter()
    m.learn_records(day(0))
    r = m.report(90)
    assert r["round_trip_pct"] is None and 0 < r["charge_points"] < NEEDED_POINTS


def test_gaps_and_the_ends_of_the_soc_range_are_left_out():
    m = EfficiencyMeter()
    assert m.learn_records(day(0, covered=0.5)) == 0
    assert m.learn_records(day(1, lo=60.0)) < m.learn_records(day(1))        # above 95 %: skipped
    recs = day(2)
    del recs[30]                                                              # a missing quarter
    assert EfficiencyMeter().learn_records(recs) == EfficiencyMeter().learn_records(day(2)) - 2
    assert EfficiencyMeter.from_json({"kwh": "x"}).report(90)["round_trip_pct"] is None


def test_old_measurements_fade():
    m = EfficiencyMeter()
    for d in range(10):
        m.learn_records(day(d))
    before = m.points["out"]
    for _ in range(60):
        m.learn_records([])
    assert m.points["out"] == pytest.approx(before / 2, rel=0.01)
