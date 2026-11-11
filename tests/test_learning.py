"""Tree-shade learning, solar forecast, house-load profile, quarter-hour meter and peaks."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from pilot.load import LoadProfile
from pilot.meter import PeakTracker, QuarterMeter, QuarterRecord, history_to_hourly
from pilot.solar import PvForecast, ShadeModel, archive_forecast, hourly_from_daily, parse_energy_forecast

BXL = ZoneInfo("Europe/Brussels")


def local(*a):
    return datetime(*a, tzinfo=BXL).astimezone(timezone.utc)


# ---------------------------------------------------------------- shade
def test_winter_tree_shade_and_summer_sun_are_learned_separately():
    m = ShadeModel()
    for _ in range(30):
        m.learn(1, 15, actual_kwh=0.15, forecast_kwh=0.5)    # January 15:00: low sun behind the trees
        m.learn(6, 12, actual_kwh=1.9, forecast_kwh=2.0)     # June noon: clear
    assert m.factor(1, 15) == pytest.approx(0.3, abs=0.03)
    assert m.factor(6, 12) == pytest.approx(0.95, abs=0.02)
    assert m.factor(6, 15) == pytest.approx(1.0)             # nothing learned there
    assert m.month_summary()[0] == pytest.approx(0.3, abs=0.01)


def test_month_without_data_borrows_from_its_neighbours():
    m = ShadeModel()
    for _ in range(40):
        m.learn(12, 15, 0.15, 0.5)
    assert 0.3 < m.factor(1, 15) < 0.8
    assert m.factor(3, 15) == pytest.approx(1.0)


def test_bug3_missing_pv_sensor_does_not_teach_darkness():
    """Old playbook: an unavailable PV sensor gave 0 kWh and the correction collapsed to 0.05."""
    m = ShadeModel()
    start = local(2026, 1, 14, 12)
    recs = [QuarterRecord(start + timedelta(minutes=15 * i), {"import": 0, "export": 0, "load": 0.1, "pv": 0.0},
                          {"import": 1, "export": 1, "load": 1, "pv": 0.0}) for i in range(4)]
    archive = {start.isoformat(): 1.2}
    assert m.learn_records(recs, archive, BXL) == 0
    assert m.factor(1, 13) == 1.0


def test_an_hour_with_blocked_export_does_not_teach_shade():
    """Export blocked at a negative price: the inverter held the panels back, the trees did not."""
    m = ShadeModel()
    start = local(2026, 6, 14, 13)
    recs = [QuarterRecord(start + timedelta(minutes=15 * i), {"import": 0, "export": 0, "load": 0.1, "pv": 0.1},
                          {"import": 1, "export": 1, "load": 1, "pv": 1.0}, blocked=i == 2) for i in range(4)]
    assert m.learn_records(recs, {start.isoformat(): 4.0}, BXL) == 0
    assert m.factor(6, 13) == 1.0
    for r in recs:
        r.blocked = False
    assert m.learn_records(recs, {start.isoformat(): 4.0}, BXL) == 1        # (the same hour, not blocked)
    assert QuarterRecord.from_json(QuarterRecord(start, {}, {}, blocked=True).to_json()).blocked


def test_learn_from_quarters_with_archived_forecast():
    m = ShadeModel()
    start = local(2026, 1, 14, 14)
    recs = [QuarterRecord(start + timedelta(minutes=15 * i), {"import": 0, "export": 0, "load": 0.1, "pv": 0.1},
                          {"import": 1, "export": 1, "load": 1, "pv": 1.0}) for i in range(4)]
    assert m.learn_records(recs, {start.isoformat(): 1.0}, BXL) == 1
    assert m.factors[0][14] == pytest.approx(0.4)


def test_ratio_is_clamped_and_old_data_fades():
    m = ShadeModel()
    m.learn(3, 10, 0.0, 1.0)
    assert m.factors[2][10] == pytest.approx(0.05)
    for _ in range(200):
        m.learn(3, 10, 0.9, 1.0)
    assert m.factors[2][10] == pytest.approx(0.9, abs=0.01)
    assert m.weights[2][10] <= 30.0
    assert ShadeModel.from_json(m.to_json()).factor(3, 10) == pytest.approx(m.factor(3, 10), abs=1e-3)


# ---------------------------------------------------------------- forecast
def test_energy_forecast_from_two_planes_is_summed():
    res = {"a": {"wh_hours": {"2026-06-15T10:00:00+00:00": 1000, "2026-06-15T11:00:00+00:00": 1500}},
           "b": {"wh_hours": {"2026-06-15T10:00:00+00:00": 500}}}
    hourly = parse_energy_forecast(res)
    assert hourly[datetime(2026, 6, 15, 10, tzinfo=timezone.utc)] == pytest.approx(1.5)
    assert hourly[datetime(2026, 6, 15, 11, tzinfo=timezone.utc)] == pytest.approx(1.5)


def test_daily_total_spread_over_daylight():
    jan = hourly_from_daily(date(2026, 1, 14), 3.0, 50.85, 4.35, BXL)
    jun = hourly_from_daily(date(2026, 6, 15), 30.0, 50.85, 4.35, BXL)
    assert sum(jan.values()) == pytest.approx(3.0)
    assert sum(jun.values()) == pytest.approx(30.0)
    assert len(jan) < len(jun)                                  # shorter days in winter
    peak = max(jun, key=jun.get).astimezone(BXL).hour
    assert 12 <= peak <= 14                                     # around solar noon (DST)
    assert all(6 <= h.astimezone(BXL).hour <= 22 for h in jun)


def test_corrected_forecast_applies_shade_per_hour_and_overlap():
    shade = ShadeModel()
    for _ in range(40):
        shade.learn(1, 15, 0.2, 1.0)
    h15 = local(2026, 1, 14, 15)
    h12 = local(2026, 1, 14, 12)
    fc = PvForecast({h12: 2.0, h15: 1.0}, shade, BXL)
    assert fc.kwh(h12, h12 + timedelta(minutes=30)) == pytest.approx(1.0, abs=0.01)
    assert fc.kwh(h15, h15 + timedelta(hours=1)) == pytest.approx(0.2, abs=0.02)
    assert fc.day_total(date(2026, 1, 14), corrected=False) == pytest.approx(3.0)
    assert fc.until == h15 + timedelta(hours=1)


def test_forecast_archive_keeps_future_hours_and_prunes():
    now = datetime(2026, 1, 14, 12, tzinfo=timezone.utc)
    arch = {(now - timedelta(days=4)).isoformat(): 1.0, (now - timedelta(hours=1)).isoformat(): 0.5}
    new = archive_forecast(arch, {now - timedelta(hours=1): 9.0, now + timedelta(hours=1): 0.7}, now)
    assert (now - timedelta(days=4)).isoformat() not in new
    assert new[(now - timedelta(hours=1)).isoformat()] == 0.5      # started hours are not overwritten
    assert new[(now + timedelta(hours=1)).isoformat()] == 0.7


# ---------------------------------------------------------------- load
def test_load_profile_learns_weekday_hours_and_falls_back():
    p = LoadProfile()
    assert p.kwh(local(2026, 1, 14, 0), local(2026, 1, 15, 0), BXL) == pytest.approx(0.55 * 24)
    for _ in range(20):
        p.learn(2, 18, 2.0)              # Wednesday 18:00
    assert p.kw_at(2, 18) == pytest.approx(2.0, abs=0.01)
    assert p.kw_at(4, 18) == pytest.approx(2.0, abs=0.01)    # other weekday borrows the same hour
    assert p.kwh(local(2026, 1, 14, 18), local(2026, 1, 14, 18, 30), BXL) == pytest.approx(1.0, abs=0.01)
    assert LoadProfile.from_json(p.to_json()).kw_at(2, 18) == pytest.approx(2.0, abs=0.01)


def test_load_learns_only_complete_hours():
    p = LoadProfile()
    start = local(2026, 1, 14, 18)
    good = [QuarterRecord(start + timedelta(minutes=15 * i), {"import": 0, "export": 0, "load": 0.5, "pv": 0},
                          {"import": 1, "export": 1, "load": 1.0, "pv": 1}) for i in range(4)]
    bad = [QuarterRecord(start + timedelta(hours=1, minutes=15 * i), {"import": 0, "export": 0, "load": 0.1, "pv": 0},
                         {"import": 1, "export": 1, "load": 0.3, "pv": 1}) for i in range(4)]
    assert p.learn_records(good + bad, BXL) == 1
    assert p.kw[2][18] == pytest.approx(2.0)
    assert p.kw[2][19] is None


# ---------------------------------------------------------------- meter and peaks
def _run(meter, start, minutes, kw, step_s=10, soc=50):
    closed = []
    t = start
    while t < start + timedelta(minutes=minutes):
        closed += meter.add(t, kw, soc)
        t += timedelta(seconds=step_s)
    return closed, t


def test_quarters_are_integrated_and_closed():
    m = QuarterMeter()
    closed, t = _run(m, local(2026, 1, 14, 18), 31, {"import": 2.0, "export": 0.0, "load": 2.5, "pv": None})
    assert len(closed) == 2
    assert closed[0].kwh["import"] == pytest.approx(0.5, abs=0.01)
    assert closed[0].kwh["load"] == pytest.approx(0.625, abs=0.01)
    assert closed[0].covered["import"] == pytest.approx(1.0, abs=0.02)
    assert closed[0].covered["pv"] == 0.0
    assert m.projection_kw(t, 2.0) == pytest.approx(2.0, abs=0.05)
    assert m.projection_kw(t, 4.0) > 3.5


def test_gap_is_not_filled_in():
    m = QuarterMeter()
    start = local(2026, 1, 14, 18)
    _run(m, start, 5, {"import": 2.0, "export": 0, "load": 2, "pv": 0})
    closed = m.add(start + timedelta(hours=2), {"import": 2.0, "export": 0, "load": 2, "pv": 0})
    assert len(closed) == 1 and closed[0].covered["import"] < 0.4
    assert m.current == start + timedelta(hours=2)


def test_bug8_monthly_peak_resets_by_month_even_after_downtime():
    """Old playbook: reset only if HA ran at 00:00:10 on the 1st."""
    pk = PeakTracker()
    rec = lambda t, kwh: QuarterRecord(t, {"import": kwh, "export": 0, "load": 0, "pv": 0},
                                       {"import": 1, "export": 1, "load": 1, "pv": 1})
    pk.add(rec(local(2026, 11, 30, 23, 45), 1.0), BXL)       # 4 kW on the last quarter of November
    pk.add(rec(local(2026, 12, 1, 9, 0), 0.5), BXL)          # first data in December comes later
    assert pk.months == {"2026-11": 4.0, "2026-12": 2.0}
    assert pk.month_peak(local(2026, 12, 2, 0), BXL) == 2.0
    assert pk.rolling_average(2.5) == pytest.approx((4.0 + 2.5) / 2)
    pk.add(rec(local(2026, 12, 3, 9, 0), 0.1), BXL)
    pk.add(QuarterRecord(local(2026, 12, 3, 9, 15), {"import": 9, "export": 0, "load": 0, "pv": 0},
                         {"import": 0.2, "export": 1, "load": 1, "pv": 1}), BXL)   # mostly missing: ignored
    assert pk.months["2026-12"] == 2.0


def test_history_to_hourly_handles_unavailable():
    t0 = datetime(2026, 1, 14, 10, tzinfo=timezone.utc)
    samples = [(t0, "1000"), (t0 + timedelta(minutes=30), "unavailable"), (t0 + timedelta(minutes=45), "2000")]
    out = history_to_hourly(samples, 0.001, t0, t0 + timedelta(hours=2))
    kwh, cov = out[t0]
    assert kwh == pytest.approx(0.5 + 0.5)        # 1 kW x 30 min + 2 kW x 15 min
    assert cov == pytest.approx(0.75)
    assert out[t0 + timedelta(hours=1)] == (pytest.approx(2.0), 1.0)
