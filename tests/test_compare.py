"""The nightly replay of one day per contract (compare.replay_day)."""
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from pilot.compare import replay_day
from pilot.config import Options
from pilot.load import LoadProfile
from pilot.meter import QuarterRecord
from pilot.prices import PriceSlot
from pilot.tariffs import DynamicTariff

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
