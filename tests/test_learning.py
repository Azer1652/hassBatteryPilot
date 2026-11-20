"""Tree-shade learning, solar forecast, house-load profile, quarter-hour meter and peaks."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from pilot.compare import Comparison
from pilot.config import Options
from pilot.load import LoadProfile
from pilot.meter import PeakTracker, QuarterMeter, QuarterRecord, history_to_hourly
from pilot.solar import (
    PvForecast,
    ShadeModel,
    archive_forecast,
    hourly_from_daily,
    intraday_factor,
    parse_energy_forecast,
    week_of,
    winter_time,
)

BXL = ZoneInfo("Europe/Brussels")


def local(*a):
    return datetime(*a, tzinfo=BXL).astimezone(timezone.utc)


# ---------------------------------------------------------------- shade
def test_winter_tree_shade_and_summer_sun_are_learned_separately():
    m = ShadeModel()
    for _ in range(30):
        m.learn(date(2026, 1, 15), 15, actual_kwh=0.15, forecast_kwh=0.5)    # January 15:00: low sun behind the trees
        m.learn(date(2026, 6, 15), 12, actual_kwh=1.9, forecast_kwh=2.0)     # June noon: clear
    assert m.factor(date(2026, 1, 15), 15) == pytest.approx(0.3, abs=0.03)
    assert m.factor(date(2026, 6, 15), 12) == pytest.approx(0.95, abs=0.02)
    assert m.factor(date(2026, 6, 15), 15) == pytest.approx(1.0)             # nothing learned there
    assert m.month_summary()[0] == pytest.approx(0.3, abs=0.01)
    assert m.month_summary()[2] is None


def test_a_week_without_data_borrows_from_the_weeks_around_it():
    m = ShadeModel()
    for _ in range(40):
        m.learn(date(2026, 12, 10), 15, 0.15, 0.5)
    assert m.factor(date(2026, 12, 24), 15) == pytest.approx(0.3, abs=0.05)    # two weeks on
    assert m.factor(date(2027, 1, 2), 15) < 0.6                                  # over new year too
    assert m.factor(date(2027, 1, 20), 15) == pytest.approx(1.0)                 # six weeks on: too far


def test_the_correction_follows_the_season_day_by_day():
    """The trees let more through every day of March as the sun climbs: no step on 1 April."""
    m = ShadeModel()
    for i in range(31):
        m.learn(date(2026, 3, 1 + i), 9, 0.4 + 0.5 * i / 30, 1.0)
    f = [m.factor(date(2026, 3, 1) + timedelta(days=i), 9) for i in range(0, 35)]
    assert f[4] < f[14] < f[24]
    assert all(abs(b - a) < 0.03 for a, b in zip(f, f[1:]))                      # no jumps, 31 March to 1 April too
    assert 0.55 < m.factor(date(2026, 3, 16), 9) < 0.75


def test_hours_are_on_winter_time_so_the_clock_change_keeps_the_sun_in_its_cell():
    assert winter_time(local(2026, 3, 26, 9), BXL) == (date(2026, 3, 26), 9)    # CET
    assert winter_time(local(2026, 3, 30, 10), BXL) == (date(2026, 3, 30), 9)   # CEST: the same sun
    m = ShadeModel()
    for _ in range(20):
        m.learn(*winter_time(local(2026, 3, 26, 9), BXL), 0.3, 1.0)
    assert m.at(local(2026, 3, 30, 10), BXL) < 0.4
    assert m.at(local(2026, 3, 30, 9), BXL) == pytest.approx(1.0)


def test_old_learning_fades_by_half_in_a_year():
    m = ShadeModel()
    m.learn(date(2026, 1, 15), 12, 0.5, 1.0)
    k = week_of(date(2026, 1, 15))
    m.fade_to(date(2026, 1, 15))
    m.fade_to(date(2026, 1, 15))                    # (twice the same night: once)
    assert m.weights[k][12] == pytest.approx(1.0)
    m.fade_to(date(2027, 1, 15))
    assert m.weights[k][12] == pytest.approx(0.5)
    m.fade_to(date(2026, 6, 1))                     # (a day before the last fade: nothing)
    assert m.weights[k][12] == pytest.approx(0.5)
    again = ShadeModel.from_json(m.to_json())
    assert again.faded == date(2027, 1, 15) and again.factor(date(2026, 1, 15), 12) == pytest.approx(
        m.factor(date(2026, 1, 15), 12), abs=1e-3)


def test_the_month_model_of_2_21_is_carried_over():
    factors, weights = [[1.0] * 24 for _ in range(12)], [[0.0] * 24 for _ in range(12)]
    factors[0][15], weights[0][15] = 0.3, 30.0          # January 15:00 behind the trees
    factors[6][14], weights[6][14] = 0.6, 30.0          # July 14:00 on the clock (summer time)
    m = ShadeModel.from_json({"factors": factors, "weights": weights}, BXL)
    assert m.migrated and len(m.factors) == 52
    assert m.factor(date(2026, 1, 15), 15) == pytest.approx(0.3, abs=0.02)
    assert m.at(local(2026, 7, 15, 14), BXL) == pytest.approx(0.6, abs=0.02)      # still 14:00 on the clock
    assert m.factor(date(2026, 7, 15), 14) == pytest.approx(1.0)
    total = sum(m.weights[k][15] for k in range(52) if m.factors[k][15] == 0.3)
    assert total == pytest.approx(30.0, rel=0.1)       # as much evidence as before
    assert not ShadeModel.from_json(m.to_json()).migrated


def test_bug3_missing_pv_sensor_does_not_teach_darkness():
    """Old playbook: an unavailable PV sensor gave 0 kWh and the correction collapsed to 0.05."""
    m = ShadeModel()
    start = local(2026, 1, 14, 12)
    recs = [QuarterRecord(start + timedelta(minutes=15 * i), {"import": 0, "export": 0, "load": 0.1, "pv": 0.0},
                          {"import": 1, "export": 1, "load": 1, "pv": 0.0}) for i in range(4)]
    archive = {start.isoformat(): 1.2}
    assert m.learn_records(recs, archive, BXL) == 0
    assert m.at(start, BXL) == 1.0


def test_an_hour_with_blocked_export_does_not_teach_shade():
    """Export blocked at a negative price: the inverter held the panels back, the trees did not."""
    m = ShadeModel()
    start = local(2026, 6, 14, 13)
    recs = [QuarterRecord(start + timedelta(minutes=15 * i), {"import": 0, "export": 0, "load": 0.1, "pv": 0.1},
                          {"import": 1, "export": 1, "load": 1, "pv": 1.0}, blocked=i == 2) for i in range(4)]
    assert m.learn_records(recs, {start.isoformat(): 4.0}, BXL) == 0
    assert m.at(start, BXL) == 1.0
    for r in recs:
        r.blocked = False
    assert m.learn_records(recs, {start.isoformat(): 4.0}, BXL) == 1        # (the same hour, not blocked)
    assert QuarterRecord.from_json(QuarterRecord(start, {}, {}, blocked=True).to_json()).blocked


def test_an_hour_at_the_inverter_ac_output_or_the_export_limit_does_not_teach_shade():
    """6.5 kWp on a 5 kW inverter with a full battery: the panels give what the inverter can pass
    (house + export at 5 kW), not what the sun offers. That is not shade."""
    start = local(2026, 6, 14, 13)

    def hour(export):
        return [QuarterRecord(start + timedelta(minutes=15 * i), {"import": 0, "export": export, "load": 0.1,
                                                                   "pv": export + 0.1},
                              {"import": 1, "export": 1, "load": 1, "pv": 1}) for i in range(4)]
    archive = {start.isoformat(): 6.5}
    at_ac = hour(4.9 / 4)                              # 0.1 + 4.9 = 5 kW out of the inverter
    assert ShadeModel().learn_records(at_ac, archive, BXL) == 1                     # (without the setting: learned)
    m = ShadeModel()
    assert m.learn_records(at_ac, archive, BXL, export_limit_kw=5.0, ac_max_kw=5.0) == 0
    assert m.at(start, BXL) == 1.0
    assert ShadeModel().learn_records(hour(4.0 / 4), archive, BXL, export_limit_kw=5.0, ac_max_kw=5.0) == 1
    at_export = hour(4.9 / 4)
    assert ShadeModel().learn_records(at_export, archive, BXL, export_limit_kw=5.0) == 0
    for r in at_ac:                                    # a grid charge in the same quarter: not at the output
        r.kwh["import"] = 1.0
    assert ShadeModel().learn_records(at_ac, archive, BXL, ac_max_kw=5.0) == 1


def test_learn_from_quarters_with_archived_forecast():
    m = ShadeModel()
    start = local(2026, 1, 14, 14)
    recs = [QuarterRecord(start + timedelta(minutes=15 * i), {"import": 0, "export": 0, "load": 0.1, "pv": 0.1},
                          {"import": 1, "export": 1, "load": 1, "pv": 1.0}) for i in range(4)]
    assert m.learn_records(recs, {start.isoformat(): 1.0}, BXL) == 1
    assert m.factors[week_of(date(2026, 1, 14))][14] == pytest.approx(0.4)


def test_ratio_is_clamped_and_old_data_fades():
    m, day = ShadeModel(), date(2026, 3, 10)
    k = week_of(day)
    m.learn(day, 10, 0.0, 1.0)
    assert m.factors[k][10] == pytest.approx(0.05)
    for _ in range(200):
        m.learn(day, 10, 0.9, 1.0)
    assert m.factors[k][10] == pytest.approx(0.9, abs=0.01)
    assert m.weights[k][10] <= 30.0
    assert ShadeModel.from_json(m.to_json()).factor(day, 10) == pytest.approx(m.factor(day, 10), abs=1e-3)


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
        shade.learn(date(2026, 1, 14), 15, 0.2, 1.0)
    h15 = local(2026, 1, 14, 15)
    h12 = local(2026, 1, 14, 12)
    fc = PvForecast({h12: 2.0, h15: 1.0}, shade, BXL)
    assert fc.kwh(h12, h12 + timedelta(minutes=30)) == pytest.approx(1.0, abs=0.01)
    assert fc.kwh(h15, h15 + timedelta(hours=1)) == pytest.approx(0.2, abs=0.02)
    assert fc.day_total(date(2026, 1, 14), corrected=False) == pytest.approx(3.0)
    assert fc.until == h15 + timedelta(hours=1)


# ---------------------------------------------------------------- today's weather against the forecast
def quarters(start, hours, kw, **kw_rec):
    """Closed quarters from `start` with `kw` of solar."""
    return [QuarterRecord(start + timedelta(minutes=15 * i), {"import": 0.0, "export": 0.0, "load": 0.1, "pv": kw / 4},
                          {"import": 1.0, "export": 1.0, "load": 1.0, "pv": kw_rec.get("cov", 1.0)}, None,
                          blocked=kw_rec.get("blocked", False)) for i in range(int(hours * 4))]


def june_day():
    return {local(2026, 6, 15, h): 2.0 for h in range(8, 20)}       # 2 kWh an hour all day


def test_a_cloudy_morning_lowers_the_rest_of_today_and_fades():
    fc = PvForecast(june_day(), ShadeModel(), BXL)
    now = local(2026, 6, 15, 12)
    f = fc.observe(quarters(local(2026, 6, 15, 9), 3, 1.0), now)    # half of what was forecast for 3 hours
    assert f == pytest.approx(0.54, abs=0.02)                        # (pulled a little towards 1)
    assert fc.corrected(local(2026, 6, 15, 12)) == pytest.approx(2.0 * f, abs=0.01)     # the hour that starts
    later = fc.corrected(local(2026, 6, 15, 16))                     # 4 hours on: half the correction
    assert later == pytest.approx(2.0 * (1 + (f - 1) / 2), abs=0.01)
    assert fc.corrected(local(2026, 6, 15, 10)) == 2.0               # hours that are over are not rewritten
    assert fc.kwh(local(2026, 6, 16, 8), local(2026, 6, 16, 9)) == 0.0   # (no forecast for tomorrow here)


def test_tomorrow_is_not_corrected_by_todays_weather():
    hourly = {**june_day(), **{local(2026, 6, 16, h): 2.0 for h in range(8, 20)}}
    fc = PvForecast(hourly, ShadeModel(), BXL)
    fc.observe(quarters(local(2026, 6, 15, 9), 3, 1.0), local(2026, 6, 15, 12))
    assert fc.day_total(date(2026, 6, 16)) == pytest.approx(24.0)
    assert fc.day_total(date(2026, 6, 15)) < 20.0


def test_a_clear_day_raises_the_forecast_within_a_limit():
    fc = PvForecast(june_day(), ShadeModel(), BXL)
    assert fc.observe(quarters(local(2026, 6, 15, 9), 3, 2.6), local(2026, 6, 15, 12)) == pytest.approx(1.28, abs=0.02)
    assert fc.observe(quarters(local(2026, 6, 15, 9), 3, 8.0), local(2026, 6, 15, 12)) == 1.6
    assert fc.observe(quarters(local(2026, 6, 15, 9), 3, 0.0), local(2026, 6, 15, 12)) == 0.3


def test_dawn_and_missing_data_say_nothing_about_the_day():
    shade, now = ShadeModel(), local(2026, 6, 15, 12)
    hourly = june_day()
    assert intraday_factor([], hourly, shade, now, BXL) == (1.0, 0.0)
    dawn = {local(2026, 6, 15, 6): 0.08}
    f, expected = intraday_factor(quarters(local(2026, 6, 15, 6), 1, 0.0), dawn, shade, local(2026, 6, 15, 7), BXL)
    assert f > 0.85 and expected == pytest.approx(0.08)
    # quarters with gaps, with export blocked, or at the export limit: the panels were not free
    assert intraday_factor(quarters(local(2026, 6, 15, 9), 3, 0.2, cov=0.5), hourly, shade, now, BXL)[0] == 1.0
    assert intraday_factor(quarters(local(2026, 6, 15, 9), 3, 0.2, blocked=True), hourly, shade, now, BXL)[0] == 1.0
    clipped = quarters(local(2026, 6, 15, 9), 3, 0.2)
    for r in clipped:
        r.kwh["export"] = 5.0 / 4
    assert intraday_factor(clipped, hourly, shade, now, BXL, export_limit_kw=5.0)[0] == 1.0
    at_ac = quarters(local(2026, 6, 15, 9), 3, 0.2)
    for r in at_ac:                                    # house + export = the inverter's 5 kW
        r.kwh["load"], r.kwh["export"] = 1.0 / 4, 4.0 / 4
    assert intraday_factor(at_ac, hourly, shade, now, BXL, export_limit_kw=5.0)[0] < 1.0
    assert intraday_factor(at_ac, hourly, shade, now, BXL, export_limit_kw=5.0, ac_max_kw=5.0)[0] == 1.0
    # yesterday's quarters and the quarter still running do not count
    assert intraday_factor(quarters(local(2026, 6, 14, 9), 3, 0.2), hourly, shade, now, BXL)[0] == 1.0
    assert intraday_factor(quarters(local(2026, 6, 15, 12), 0.25, 0.0), hourly, shade, now, BXL)[0] == 1.0


def test_the_correction_is_on_top_of_the_learned_shade():
    shade = ShadeModel()
    for h in range(8, 20):
        for _ in range(40):
            shade.learn(*winter_time(local(2026, 6, 15, h), BXL), 0.5, 1.0)   # trees: half of the forecast, always
    fc = PvForecast(june_day(), shade, BXL)
    f = fc.observe(quarters(local(2026, 6, 15, 9), 3, 1.0), local(2026, 6, 15, 12))     # as the shade predicts
    assert f == pytest.approx(1.0, abs=0.03)


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


def test_a_partly_measured_quarter_counts_at_its_measured_power():
    """PeakTracker.add and Comparison.add_quarter take kWh x 4 without scaling by the share of the quarter measured."""
    t = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc)
    rec = QuarterRecord(t, {"import": 0.6, "export": 0.0, "load": 0.6, "pv": 0.0},       # 4 kW for 60 % of the quarter
                        {"import": 0.6, "export": 0.6, "load": 0.6, "pv": 0.6})
    peaks, cmp = PeakTracker(), Comparison()
    peaks.add(rec, BXL)
    cmp.add_quarter(rec, None, Options(), BXL)
    assert peaks.month_peak(t, BXL) == pytest.approx(4.0), f"peak tracker: {peaks.month_peak(t, BXL)} kW"
    assert cmp.months["2026-01"]["peak_kw"] == pytest.approx(4.0), f"comparison: {cmp.months['2026-01']['peak_kw']} kW"


def test_a_clock_that_steps_back_does_not_count_energy_twice():
    """QuarterMeter.add takes an earlier time as the new last sample: the overlap is integrated twice."""
    t0 = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc)
    kw = {"import": 2.0, "export": 0.0, "load": 2.0, "pv": 0.0}
    m, t = QuarterMeter(), t0
    for _ in range(30):                                                  # five minutes of 10-second samples
        m.add(t, kw)
        t += timedelta(seconds=10)
    t -= timedelta(seconds=60)                                           # the clock is set back a minute (NTP)
    closed = []
    while not closed:
        closed = m.add(t, kw)
        t += timedelta(seconds=10)
    assert closed[0].kwh["import"] == pytest.approx(0.5, abs=0.01), f"{closed[0].kwh['import']:.3f} kWh for 2 kW x 15 min"


def _meter_run(m, t, end, kw, out):
    while t < end:
        out += m.add(t, kw)
        t += timedelta(seconds=10)
    return t


def test_a_clock_set_back_five_minutes_is_waited_out():
    """The review's case: 5 minutes back at 18:10 at a steady 2 kW gave the quarter 0.661 kWh."""
    kw = {"import": 2.0, "export": 0.0, "load": 2.0, "pv": 0.0}
    m, out = QuarterMeter(), []
    t = _meter_run(m, datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc), datetime(2026, 1, 15, 18, 10, tzinfo=timezone.utc),
                   kw, out)
    _meter_run(m, t - timedelta(minutes=5), datetime(2026, 1, 15, 18, 16, tzinfo=timezone.utc), kw, out)
    assert out[0].start.minute == 0 and out[0].kwh["import"] == pytest.approx(0.5, abs=0.01)


def test_a_quarter_never_closes_twice_after_a_big_step_back():
    kw = {"import": 2.0, "export": 0.0, "load": 2.0, "pv": 0.0}
    m, out = QuarterMeter(), []
    t = _meter_run(m, datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc), datetime(2026, 1, 15, 18, 40, tzinfo=timezone.utc),
                   kw, out)
    _meter_run(m, t - timedelta(minutes=30), datetime(2026, 1, 15, 19, 0, 10, tzinfo=timezone.utc), kw, out)
    starts = [r.start for r in out]
    assert len(starts) == len(set(starts)) == 4 and starts == sorted(starts)
