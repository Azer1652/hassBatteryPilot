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


# ---------------------------------------------------------------- the inverter's own use
DRAW_KW = 0.04


def day_with_draw(d, eta=0.85 ** 0.5, draw_kw=DRAW_KW, battery=True, lo=30.0):
    """Night idle (battery 0 W, no sun), 20 points in from solar, 20 points back to the house; the
    inverter draws `draw_kw` all day on top of the house. The battery channel is DC, + discharging."""
    recs, soc, t = [], lo, T0 + timedelta(days=d)
    q_draw = draw_kw / 4
    for i in range(96):
        kwh = {c: 0.0 for c in CHANNELS}
        kwh["load"], dc = 0.1, CAP / 100
        if 20 <= i < 40:                                   # solar: 1 point a quarter in
            kwh["pv"], kwh["battery"], nxt = 0.1 + dc / eta + q_draw, -dc, soc + 1
        elif 50 <= i < 70:                                 # 1 point a quarter out, to the house
            kwh["load"], kwh["battery"], nxt = dc * eta - q_draw, dc, soc - 1
        elif 40 <= i < 50:                                 # sun, battery idle
            kwh["pv"], nxt = 0.1 + q_draw, soc
        else:                                              # night, battery idle: the grid feeds both
            kwh["import"], nxt = 0.1 + q_draw, soc
        covered = {c: 1.0 for c in CHANNELS} | ({} if battery else {"battery": 0.0})
        recs.append(QuarterRecord(t + timedelta(minutes=15 * i), kwh, covered, soc))
        soc = nxt
    return recs


def test_the_inverters_own_use_is_measured_and_left_out_of_the_round_trip():
    m = EfficiencyMeter()
    for d in range(10):
        m.learn_records(day_with_draw(d))
    r = m.report(75)
    assert r["inverter_draw_w"] == pytest.approx(40, abs=1)
    assert r["inverter_kwh_day"] == pytest.approx(0.96, abs=0.03)
    assert r["round_trip_pct"] == pytest.approx(85, abs=1)                 # the battery and its conversion
    assert r["round_trip_with_draw_pct"] == pytest.approx(74.9, abs=1)     # as measured on the AC side
    assert r["solar_round_trip_pct"] == pytest.approx(85, abs=1) and r["grid_round_trip_pct"] is None
    assert r["solar_points"] == r["charge_points"] and r["grid_points"] == 0
    assert r["since"] == "2026-06-01"
    m2 = EfficiencyMeter.from_json(m.to_json())
    assert m2.report(75) == r


def test_without_the_battery_sensor_the_round_trip_keeps_the_draw():
    m = EfficiencyMeter()
    for d in range(10):
        m.learn_records(day_with_draw(d, battery=False))
    r = m.report(75)
    assert r["inverter_draw_w"] is None and r["draw_hours"] == 0
    assert r["round_trip_pct"] == r["round_trip_with_draw_pct"] == pytest.approx(74.9, abs=1)


def test_the_draw_needs_enough_idle_hours_and_skips_quarters_that_do_not_fit():
    m = EfficiencyMeter()
    recs = day_with_draw(0)[:20]                           # 5 idle hours
    m.learn_records(recs)
    assert m.report(75)["inverter_draw_w"] is None and m.report(75)["draw_hours"] == pytest.approx(5, abs=0.1)
    odd = [QuarterRecord(r.start, r.kwh | {"import": 0.5}, r.covered, r.soc) for r in day_with_draw(1)[:20]]
    m.learn_records(odd)                                   # 2 kW beyond the house: not the inverter
    assert m.draw_hours == pytest.approx(5 * 0.5 ** (1 / 60), abs=0.1)


def test_state_from_before_the_draw_was_measured_starts_over():
    old = {"kwh": {"grid": 10.0, "solar": 20.0, "out": 25.0}, "points": {"grid": 60, "solar": 140, "out": 205}}
    m = EfficiencyMeter.from_json(old)
    r = m.report(75)
    assert r["round_trip_pct"] is None and r["charge_points"] == 0 and r["since"] is None


async def test_the_app_measures_the_draw_from_the_battery_power_sensor(tmp_path):
    from fake_ha import FakeHA, utc

    from pilot.app import App
    from pilot.config import Options

    fake = FakeHA(utc(2026, 11, 17, 20, 0), soc=10.0, load=lambda t: 0.3, battery_power=True,
                  inverter_draw_kw=DRAW_KW)
    app = App(Options(), fake, data_dir=str(tmp_path / "data"), clock=lambda: fake.now)
    assert await app.setup()
    assert app.ent_ids["battery_power"] == "sensor.solis_battery_power_net"
    end = fake.now + timedelta(hours=10)
    while fake.now < end:
        await app.step()
        fake.advance(timedelta(seconds=60))
    assert all("battery" in r.kwh and r.covered["battery"] > 0.9 for r in app.records[1:])
    app.efficiency.learn_records(app.records)
    await app.publish()
    eff = fake.published["sensor.battery_pilot_efficiency"]["attributes"]
    assert eff["inverter_draw_w"] == pytest.approx(40, abs=2)
    draw = fake.published["sensor.battery_pilot_inverter_draw"]
    assert float(draw["state"]) == pytest.approx(40, abs=2) and draw["attributes"]["unit_of_measurement"] == "W"
