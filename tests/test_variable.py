"""A variable (monthly-indexed) contract: factor x the month's market average + a fixed part."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fake_ha import TZ, FakeHA

from pilot.app import App
from pilot.compare import Comparison
from pilot.config import ConfigError, Options
from pilot.meter import QuarterRecord
from pilot.settings import RANGES
from pilot.tariffs import VariableTariff, kinds, tariff

BXL = ZoneInfo("Europe/Brussels")
# A made-up formula in the shape of a Belgian tariff sheet (not a real supplier's numbers)
VAR = dict(variable_day_mult=0.1, variable_day_add=1.5, variable_night_mult=0.09, variable_night_add=1.2,
           variable_vat_pct=6, variable_sell_mult=0.08, variable_sell_add=-1.0, variable_yearly_fee_eur=30)


def test_the_price_is_the_formula_at_the_months_index():
    o = Options(**VAR)
    t = tariff("variable", o, 80.0)
    day = datetime(2026, 11, 18, 12, 0, tzinfo=BXL)           # a Wednesday noon
    night = datetime(2026, 11, 18, 23, 0, tzinfo=BXL)
    assert t.buy(day, 300.0) == pytest.approx((0.1 * 80 + 1.5) * 1.06 + o.grid_fees_c_kwh)   # the hour's price is ignored
    assert t.buy(night, -20.0) == pytest.approx((0.09 * 80 + 1.2) * 1.06 + o.grid_fees_c_kwh)
    assert t.sell(day, 300.0) == pytest.approx(0.08 * 80 - 1.0 - o.injection_fee_c_kwh)
    assert t.sell(day, None, 100.0) == t.sell(day, 300.0)       # the index is the month's, not the argument
    assert t.yearly_fee_eur == 30


def test_a_night_left_empty_uses_the_day_formula():
    """Only the day factor filled in (a single-rate meter): the night is not priced at the grid fees alone."""
    o = Options(variable_day_mult=0.1, variable_day_add=1.5)
    t = tariff("variable", o, 80.0)
    assert t.rate(True) == t.rate(False) > o.grid_fees_c_kwh + 5
    Options(contract="variable", variable_day_mult=0.1).validate()
    assert tariff("variable", o).index() is None        # no prices, nothing set: no index


def test_a_published_index_needs_its_month():
    with pytest.raises(ConfigError):
        Options(**VAR, variable_index_eur_mwh=120.0).validate()
    with pytest.raises(ConfigError):
        Options(**VAR, variable_index_eur_mwh=120.0, variable_index_month="2026-13").validate()
    Options(**VAR, variable_index_eur_mwh=120.0, variable_index_month="2026-11").validate()


def test_variable_is_compared_only_when_it_is_filled_in():
    assert kinds(Options()) == ("fixed", "dynamic")
    assert kinds(Options(**VAR)) == ("fixed", "dynamic", "variable")
    with pytest.raises(ConfigError):
        Options(contract="variable").validate()          # the formula is missing
    Options(contract="variable", **VAR).validate()


def test_the_settings_ranges_match_the_schema():
    for k in VAR:
        assert k in RANGES
    assert "variable_index_eur_mwh" in RANGES


def _quarter(h, kwh=0.25, exp=0.1):
    t = datetime(2026, 6, 10, h, tzinfo=BXL).astimezone(timezone.utc)
    return QuarterRecord(t, {"import": kwh, "export": exp, "load": kwh, "pv": 0.0},
                         {"import": 1, "export": 1, "load": 1, "pv": 1}, 50)


def test_same_usage_prices_the_variable_contract_at_the_months_average():
    o = Options(**VAR)
    c = Comparison()
    for h in range(24):
        c.add_quarter(_quarter(h), 50.0 if h < 12 else 150.0, o, BXL)      # average 100 EUR/MWh
    r = c.report("2026-06", o)
    var = VariableTariff(o, 100.0)
    share = 24 / (30 * 96)
    cap = 2.5 * o.capacity_tariff_eur_kw_year / 12 * share
    # (a Wednesday: night is before 07:00 and from 22:00)
    night_h = sum(1 for h in range(24) if h < 7 or h >= 22)
    energy = (0.25 * (24 - night_h) * var.rate(False) + 0.25 * night_h * var.rate(True) - 0.1 * 24 * var.sell(None)) / 100
    assert r["same_usage"]["variable_eur"] == pytest.approx(energy + 30 / 12 * share + cap, abs=0.01)
    s = c.summary(o, datetime(2026, 6, 11, tzinfo=timezone.utc), BXL)
    assert s["same_usage"]["variable_minus_fixed_eur"] == pytest.approx(
        r["same_usage"]["variable_eur"] - r["same_usage"]["fixed_eur"], abs=0.01)
    assert "variable_eur" not in c.report("2026-06", Options())["same_usage"]      # not filled in: left out


def test_a_variable_contract_filled_in_mid_month_counts_its_own_days():
    o = Options(**VAR)
    c = Comparison()
    res = {"cost_c": 300.0, "imp": 10.0, "exp": 0.0, "charged": 0.0, "discharged": 0.0, "grid_charged": 0.0,
           "peak_kw": 2.0}
    for _ in range(3):                           # three days before the variable contract was filled in
        for kind in ("fixed", "dynamic"):
            c.add_replay("2026-06", kind, res)
        c.month("2026-06")["replay_days"] += 1
    for kind in ("fixed", "dynamic", "variable"):
        c.add_replay("2026-06", kind, res)
    c.month("2026-06")["replay_days"] += 1
    ws = c.report("2026-06", o)["with_strategy"]
    assert ws["fixed"]["days"] == 4 and ws["variable"]["days"] == 1
    cap = 2.5 * o.capacity_tariff_eur_kw_year / 12
    assert ws["variable"]["eur"] == pytest.approx(3.0 + (30 / 12 + cap) / 30, abs=0.01)


def test_variable_against_fixed_covers_the_same_days():
    """Filled in mid-month with the same daily cost as fixed: the difference is the fee only, not 3 days of fixed."""
    o = Options(**VAR, fixed_yearly_fee_eur=30)          # the same fee as the variable contract
    c = Comparison()
    res = {"cost_c": 300.0, "imp": 10.0, "exp": 0.0, "charged": 0.0, "discharged": 0.0, "grid_charged": 0.0,
           "peak_kw": 2.0}
    for day in range(4):
        for kind in ("fixed", "dynamic") + (("variable",) if day == 3 else ()):
            c.add_replay("2026-06", kind, res, res if kind == "variable" else None)
        c.month("2026-06")["replay_days"] += 1
    ws = c.summary(o, datetime(2026, 6, 5, tzinfo=timezone.utc), BXL)["with_strategy"]
    assert ws["variable_days"] == 1
    assert ws["variable_minus_fixed_eur"] == pytest.approx(0.0, abs=0.01)


def test_a_month_saved_before_the_variable_contract_still_reports():
    old = {"months": {"2026-06": {"imp_day": 1.0, "replay_days": 2,
                                  "replay": {k: {"cost_c": 100.0, "imp": 1.0, "exp": 0.0, "charged": 0.0,
                                                 "discharged": 0.0, "grid_charged": 0.0, "stored_c": 0.0,
                                                 "peak_kw": 1.0} for k in ("fixed", "dynamic")}}}}
    r = Comparison.from_json(old).report("2026-06", Options(**VAR))
    assert set(r["with_strategy"]) == {"fixed", "dynamic"}          # no variable days yet
    assert r["with_strategy"]["fixed"]["days"] == 2


async def test_a_published_index_stays_with_its_month(tmp_path):
    """Setting June's index changes neither May nor July, and stays with June after July's is set."""
    fake = FakeHA(datetime(2026, 6, 20, 12, tzinfo=timezone.utc))
    o = Options(**VAR)
    app = App(o, fake, data_dir=str(tmp_path / "data"), clock=lambda: fake.now)
    for key in ("2026-05", "2026-06"):
        for h in range(24):
            t = datetime(int(key[:4]), int(key[5:]), 10, h, tzinfo=BXL).astimezone(timezone.utc)
            app.comparison.add_quarter(QuarterRecord(t, {"import": 0.25, "export": 0.0, "load": 0.25, "pv": 0.0},
                                                     {"import": 1, "export": 1, "load": 1, "pv": 1}, 50), 80.0, o, BXL)
    before = {k: app.comparison.report(k, o)["same_usage"]["variable_eur"] for k in ("2026-05", "2026-06")}
    o.variable_index_eur_mwh, o.variable_index_month = 150.0, "2026-06"
    app._keep_variable_index()
    after = {k: app.comparison.report(k, o)["same_usage"]["variable_eur"] for k in ("2026-05", "2026-06")}
    assert after["2026-05"] == before["2026-05"] and after["2026-06"] > before["2026-06"]
    assert app.variable_index(datetime(2026, 6, 20, tzinfo=timezone.utc)) == 150.0
    july = datetime(2026, 7, 2, 12, tzinfo=timezone.utc)
    assert app.variable_index(july) != 150.0                         # not carried into the next month
    o.variable_index_eur_mwh, o.variable_index_month = 95.0, "2026-07"
    app._keep_variable_index()
    assert app.comparison.report("2026-06", o)["same_usage"]["variable_eur"] == after["2026-06"]
    assert app.variable_index(july) == 95.0


def _prices(day, days=2):
    out = []
    for h in range(24 * days):
        t = day + timedelta(hours=h)
        out.append({"time": t.isoformat(), "price": 0.04 if h % 24 in (2, 3, 4, 13, 14) else
                    (0.25 if h % 24 in (18, 19, 20) else 0.11)})
    return out


async def test_the_app_plans_prices_and_replays_a_variable_contract(tmp_path):
    day = datetime(2026, 3, 10, tzinfo=TZ)
    start = day.astimezone(timezone.utc)
    fake = FakeHA(start - timedelta(minutes=5), soc=50, prices=_prices(day))
    o = Options(contract="variable", **VAR)
    app = App(o, fake, data_dir=str(tmp_path / "data"), clock=lambda: fake.now)
    assert await app.setup()
    await app.tick()
    assert app.plan is not None and app.plan.contract == "variable" and app.problem is None
    assert len({round(s.buy, 4) for s in app.plan.steps}) <= 2              # day and night only, not hourly
    pr = app.current_prices()
    assert pr["buy"] == pr["variable"]["buy"]
    assert app.trading["variable"] is not None
    for q in range(96):
        t = start + timedelta(minutes=15 * q)
        load = 0.6 / 4
        app.records.append(QuarterRecord(t, {"import": load, "export": 0.0, "load": load, "pv": 0.0},
                                         {"import": 1, "export": 1, "load": 1, "pv": 1}, 50))
    fake.now = start + timedelta(days=1, minutes=30)
    await app.nightly()
    r = app.comparison.report("2026-03", o)
    assert r["replay_days"] == 1 and r["with_strategy"]["variable"]["days"] == 1
    assert [e for e in app.events if e["text"].startswith("Replayed") and "variable €" in e["text"]]
    await app.publish()
    assert "variable" in fake.published["sensor.battery_pilot_buy_price"]["attributes"]


async def test_a_variable_contract_without_prices_or_index_has_no_plan(tmp_path):
    fake = FakeHA(datetime(2026, 3, 10, 12, tzinfo=timezone.utc))
    app = App(Options(contract="variable", **VAR), fake, data_dir=str(tmp_path / "data"), clock=lambda: fake.now)
    assert await app.setup()
    await app.tick()
    assert app.plan is None and app.problem.startswith("Variable contract")
    app.o.variable_index_eur_mwh, app.o.variable_index_month = 90.0, "2026-03"     # the index set by hand: it plans
    app.replan()
    assert app.plan is not None and app.problem is None
