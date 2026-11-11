"""What trading with the battery would earn (the line on the page), against the contract's own prices."""
from datetime import date, datetime, timedelta, timezone

import pytest
from fake_ha import CHEAP_NIGHT, TZ, FakeHA, utc

from pilot.app import App
from pilot.config import Options
from pilot.prices import PriceSlot
from pilot.trading import margins

NOW = datetime(2026, 6, 15, 10, tzinfo=timezone.utc)        # noon in Brussels


def market(prices: dict[int, float], base: float = 80.0, minutes: int = 60, hours: int = 24) -> list[PriceSlot]:
    """Market prices from NOW: `base`, except the slots in `prices` (slot number -> EUR/MWh)."""
    step = timedelta(minutes=minutes)
    return [PriceSlot(NOW + i * step, NOW + (i + 1) * step, prices.get(i, base)) for i in range(hours * 60 // minutes)]


def dyn(prices, **kw):
    return margins(Options(), "dynamic", market(prices, **kw), NOW, TZ, 80.0)


def test_a_trade_breaks_even_only_at_a_high_selling_price():
    """Default rates: bought at a market price of 0, a kWh must be sold near 167 EUR/MWh: the grid
    fees sit on the purchase, a tenth is lost and the battery wears."""
    even = dyn({2: 0.0, 9: 167.0})["trade"]
    assert even["margin_c"] == pytest.approx(0.0, abs=0.2)
    assert even["needed_market_eur_mwh"] == pytest.approx(167, abs=2)
    assert even["buy_c"] == pytest.approx(11.76, abs=0.01) and even["needed_sell_c"] == pytest.approx(15.2, abs=0.1)
    assert dyn({2: 0.0, 9: 100.0})["trade"]["margin_c"] < -5
    assert dyn({2: 0.0, 9: 250.0})["trade"]["margin_c"] > 5
    neg = dyn({2: -50.0, 9: 100.0})["trade"]                 # a negative price lowers the bar, not enough
    assert neg["needed_market_eur_mwh"] == pytest.approx(107, abs=3) and neg["margin_c"] < 0


def test_the_sale_comes_after_the_purchase():
    t = dyn({0: 300.0, 9: 0.0})["trade"]                      # dear first, cheap later: no trade in that order
    assert datetime.fromisoformat(t["buy_at"]) < datetime.fromisoformat(t["at"])
    assert t["margin_c"] < 0
    t = dyn({2: 0.0, 9: 300.0})["trade"]
    assert datetime.fromisoformat(t["buy_at"]) == NOW + timedelta(hours=2)
    assert datetime.fromisoformat(t["at"]) == NOW + timedelta(hours=9)


def test_shifting_needs_a_much_smaller_spread_than_trading():
    m = dyn({2: 40.0, 9: 110.0})                              # 70 EUR/MWh apart
    assert m["shift"]["margin_c"] > 2 and m["trade"]["margin_c"] < 0
    flat = dyn({})
    assert flat["shift"]["margin_c"] < 0                      # same price all day: only the losses


def test_quarter_hour_prices_are_used_as_they_are():
    m = dyn({9: 0.0, 38: 300.0}, minutes=15)
    assert datetime.fromisoformat(m["trade"]["buy_at"]) == NOW + timedelta(minutes=135)
    assert datetime.fromisoformat(m["trade"]["at"]) == NOW + timedelta(minutes=570)
    assert m["trade"]["margin_c"] > 5


def test_a_fixed_contract_never_trades_and_shifts_only_with_a_cheap_night():
    m = margins(Options(), "fixed", [], NOW, TZ, 80.0)
    assert m["trade"]["margin_c"] < -15 and "needed_market_eur_mwh" not in m["trade"]
    assert m["shift"]["margin_c"] < 0                         # default day/night rates: as `night_charge_pays`
    cheap = margins(Options(**CHEAP_NIGHT), "fixed", [], NOW, TZ, 80.0)
    assert cheap["shift"]["margin_c"] > 0
    assert datetime.fromisoformat(cheap["shift"]["buy_at"]).astimezone(TZ).hour == 22


def test_no_prices_no_answer_for_the_dynamic_contract():
    assert margins(Options(), "dynamic", [], NOW, TZ, None) is None
    assert margins(Options(), "dynamic", market({}, hours=1), NOW + timedelta(minutes=30), TZ, None) is None


def test_the_plan_agrees_with_the_margin():
    """Where the margin says a trade earns clearly, the plan charges from the grid and exports."""
    from pilot.planner import make_plan
    from pilot.tariffs import tariff
    o = Options(contract="dynamic")

    def plan(prices):
        return make_plan(NOW, TZ, o, tariff("dynamic", o), market(prices, hours=36),
                         lambda a, b: 0.3 * (b - a).total_seconds() / 3600, lambda a, b: 0.0, 40.0, market_avg=80.0)

    rich = plan({2: 0.0, 3: 0.0, 9: 400.0})
    assert dyn({2: 0.0, 3: 0.0, 9: 400.0})["trade"]["margin_c"] > 10
    assert any(s.action == "charge" for s in rich.steps) and any(s.action == "discharge" for s in rich.steps)
    poor = plan({2: 60.0, 9: 100.0})
    assert dyn({2: 60.0, 9: 100.0})["trade"]["margin_c"] < 0
    assert not any(s.action == "discharge" for s in poor.steps)


async def test_the_app_reports_the_margin_for_both_contracts(tmp_path):
    day = date(2026, 6, 15)
    prices = [{"time": (datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=h)).isoformat(),
               "price": 0.25 if h % 24 == 20 else 0.0 if h % 24 == 13 else 0.08} for h in range(72)]
    fake = FakeHA(utc(2026, 6, 15, 8, 0), soc=50, prices=prices)
    app = App(Options(contract="dynamic"), fake, data_dir=str(tmp_path / "data"), clock=lambda: fake.now)
    assert await app.setup()
    await app.step()
    t = app.status()["trading"]
    assert t["dynamic"]["trade"]["margin_c"] > 0 and t["fixed"]["trade"]["margin_c"] < 0
    assert datetime.fromisoformat(t["dynamic"]["trade"]["buy_at"]).astimezone(TZ).hour == 13
