"""Planned vs actual: plan snapshots, measured quarters, accuracy and the day files."""
import json
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from pilot import history as H
from pilot.meter import QuarterRecord
from pilot.planner import Plan, Step
from pilot.solar import PvForecast, ShadeModel

BXL = ZoneInfo("Europe/Brussels")


def local(*a):
    return datetime(*a, tzinfo=BXL).astimezone(timezone.utc)


def plan(start, socs, hours=1.0, actions=None, first_hours=None):
    """A plan whose steps go through `socs` (one more value than steps)."""
    steps, t = [], start
    for i, (a, b) in enumerate(zip(socs, socs[1:])):
        h = first_hours if (i == 0 and first_hours) else hours
        act = actions[i] if actions else "self_use"
        steps.append(Step(t, h, act, a, b, 0, 0, 0, 0, 10, 5, 0))
        t += timedelta(hours=h)
    return Plan(steps, start, "fixed")


def rec(t, soc=50.0, pv=0.25, load=0.1, cov=1.0, mode="self_use"):
    covered = {"import": cov, "export": cov, "load": cov, "pv": cov}
    return QuarterRecord(t, {"import": load * cov, "export": 0.0, "load": load * cov, "pv": pv * cov}, covered, soc, mode)


# ---------------------------------------------------------------- snapshots of the plan
def test_plan_points_start_at_a_partial_step_and_are_clipped():
    start, _ = H.day_bounds(date(2026, 1, 14), BXL)
    p = plan(local(2026, 1, 14, 0, 20), [40, 45, 50, 50, 60], first_hours=40 / 60,
             actions=["charge", "charge", "self_use", "charge"])
    assert H.plan_points(p, p.created, start + timedelta(hours=24), start) == \
        [[20, 40], [60, 45], [120, 50], [180, 50], [240, 60]]
    assert H.plan_points(p, p.created, local(2026, 1, 14, 1, 30), start) == [[20, 40], [60, 45], [90, 47.5]]
    assert H.plan_actions(p, p.created, start + timedelta(hours=24), start) == \
        [[20, 120, "charge"], [120, 180, "self_use"], [180, 240, "charge"]]


def test_plan_points_on_quarter_hour_steps():
    start, end = H.day_bounds(date(2026, 1, 14), BXL)
    p = plan(local(2026, 1, 14, 10, 0), [50, 51, 52, 53, 54], hours=0.25)
    assert H.plan_points(p, p.created, end, start) == [[600, 50], [615, 51], [630, 52], [645, 53], [660, 54]]


@pytest.mark.parametrize("day,minutes", [(date(2026, 3, 29), 23 * 60), (date(2026, 10, 25), 25 * 60),
                                         (date(2026, 6, 1), 24 * 60)])
def test_dst_days_have_their_real_length(day, minutes):
    start, end = H.day_bounds(day, BXL)
    assert H.minutes(end, start) == minutes
    recs = [rec(start + timedelta(minutes=15 * i), soc=50 + i * 0.1) for i in range(minutes // 15 + 1)]
    m = H.measured(recs, start, end)
    assert len(m["pv"]) == minutes // 15 and len(m["soc"]) == minutes // 15 + 1
    assert m["soc"][-1] == pytest.approx(50 + minutes // 15 * 0.1, abs=0.05)   # the SOC at the end of the day


def test_hour_ahead_keeps_the_forecast_after_the_shade_in_force():
    shade = ShadeModel()
    for _ in range(40):
        shade.learn(1, 13, 0.3, 1.0)
    hour = local(2026, 1, 14, 13)
    pv = PvForecast({hour: 1.0, hour + timedelta(hours=1): 0.5}, shade, BXL)
    start, _ = H.day_bounds(date(2026, 1, 14), BXL)
    p = plan(local(2026, 1, 14, 13, 4), [60, 58, 55], first_hours=56 / 60)
    h = H.hour_ahead(p, pv, lambda a, b: 0.4 * (b - a).total_seconds() / 3600, hour, start)
    assert h["hour"] == 13 * 60 and h["created"] == 13 * 60 + 4
    assert h["soc"] == [[784, 60], [840, 58]]
    assert h["pv_raw"] == 1.0 and h["pv"] == pytest.approx(shade.factor(1, 13), abs=1e-3)
    assert h["load"] == pytest.approx(0.4)


def test_day_ahead_has_every_hour_and_no_forecast_past_its_end():
    start, end = H.day_bounds(date(2026, 1, 14), BXL)
    noon = local(2026, 1, 14, 12)
    pv = PvForecast({noon: 2.0}, ShadeModel(), BXL)                     # forecast ends at 13:00
    p = plan(local(2026, 1, 14, 0, 20), [40] * 25, first_hours=40 / 60)
    d = H.day_ahead(p, pv, lambda a, b: 0.5, start, end)
    assert d["created"] == 20 and len(d["hours"]) == 24
    assert d["hours"][12] == [720, 2.0, 2.0, 0.5]
    assert d["hours"][11][1] == 0.0 and d["hours"][13][1] is None       # unknown, not dark


# ---------------------------------------------------------------- what happened
def test_measured_marks_gaps_and_scales_partial_quarters():
    start, end = H.day_bounds(date(2026, 1, 14), BXL)
    recs = [rec(start + timedelta(minutes=15 * i)) for i in range(96) if i != 5]
    recs[2] = rec(start + timedelta(minutes=30), cov=0.5)               # half the quarter measured
    recs[3] = rec(start + timedelta(minutes=45), cov=0.3)               # too little
    m = H.measured(recs, start, end)
    assert m["pv"][2] == pytest.approx(0.25)
    assert m["pv"][3] is None and m["pv"][5] is None and m["soc"][5] is None
    assert m["mode"][0] == "self_use" and m["mode"][5] is None
    assert m["soc"][96] is None                                         # tomorrow's first quarter not there yet


# ---------------------------------------------------------------- accuracy
def test_summary_compares_both_predictions_with_what_happened():
    start, end = H.day_bounds(date(2026, 1, 14), BXL)
    # Measured: SOC climbs 1 point per quarter from 40; solar 0.25 kWh per quarter from 10:00 to 12:00.
    recs = [rec(start + timedelta(minutes=15 * i), soc=40 + i, pv=0.25 if 40 <= i < 48 else 0.0,
                mode="shadow" if i < 24 else "charge") for i in range(97)]
    log = {"day_ahead": {"created": 0, "soc": [[0, 40], [1440, 136]],      # +1 point per quarter: exact
                         "actions": [[0, 1440, "charge"]],
                         "hours": [[60 * h, 2.0 if h in (10, 11) else 0.0, 1.5 if h in (10, 11) else 0.0, 0.5]
                                   for h in range(24)]},
             "hours": {"600": {"hour": 600, "created": 600, "soc": [[600, 80], [660, 86]], "actions": [],
                               "pv_raw": 2.0, "pv": 1.5, "load": 0.5},  # measured 84 at 11:00: off by -2
                       "660": {"hour": 660, "created": 662, "soc": [[662, 84], [720, 87]], "actions": [],
                               "pv_raw": 2.0, "pv": 1.5, "load": 0.5}}}  # measured 88 at 12:00: off by +1
    doc = H.build_day(date(2026, 1, 14), BXL, recs, log)
    s = doc["summary"]
    assert s["complete"] and s["active_share"] == 0.75
    assert s["solar_measured"] == 2.0 and s["house_measured"] == pytest.approx(9.6)
    da = s["day_ahead"]
    assert (da["solar_forecast"], da["solar_after_shade"], da["solar_measured"]) == (4.0, 3.0, 2.0)
    assert da["house_expected"] == 12.0 and da["house_measured"] == pytest.approx(9.6)
    assert da["soc_mean_abs"] == 0.0 and da["soc_end_planned"] == 136 and da["soc_end_measured"] == 136
    hr = s["hourly"]
    assert hr["hours"] == 2 and hr["solar_after_shade"] == 3.0 and hr["solar_measured"] == 2.0
    assert hr["soc_mean_abs"] == 1.5 and hr["soc_max_abs"] == -2.0
    assert [h["hour"] for h in doc["hourly"]] == [600, 660]


def test_summary_without_any_prediction():
    doc = H.build_day(date(2026, 1, 14), BXL, [], None)
    s = doc["summary"]
    assert s["day_ahead"] is None and s["hourly"] is None and not s["complete"] and s["active_share"] is None


# ---------------------------------------------------------------- storage
def test_store_writes_reads_indexes_and_prunes(tmp_path):
    store = H.HistoryStore(str(tmp_path / "history"))
    assert store.index() == [] and store.read(date(2026, 1, 1)) is None
    for d in (date(2026, 1, 1), date(2026, 3, 5), date(2026, 3, 4)):
        store.write(H.build_day(d, BXL, [], None))
    assert [s["date"] for s in store.index()] == ["2026-01-01", "2026-03-04", "2026-03-05"]
    assert store.read(date(2026, 3, 4))["date"] == "2026-03-04"
    with open(os.path.join(store.path, "2026-03-06.json"), "w") as f:
        f.write("{broken")
    with open(os.path.join(store.path, "notes.txt"), "w") as f:
        f.write("not a day")
    fresh = H.HistoryStore(store.path)                                  # rebuilt from the files
    assert [s["date"] for s in fresh.index()] == ["2026-01-01", "2026-03-04", "2026-03-05"]
    fresh.prune(date(2026, 3, 5))                                       # keeps 60 days
    assert [s["date"] for s in fresh.index()] == ["2026-03-04", "2026-03-05"]
    assert not os.path.exists(os.path.join(store.path, "2026-01-01.json"))
    with open(os.path.join(store.path, "2026-03-05.json")) as f:
        assert json.load(f)["summary"]["date"] == "2026-03-05"
