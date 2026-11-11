"""What trading with the battery would earn in the next hours, for the page.

The plan already buys in cheap hours and sells in dear ones when that pays. It rarely does: every
bought kWh carries the grid fees, part of it is lost in the two conversions and the battery wears.
`margins` shows the best pair of hours with the contract's own prices, so the page can say why
the plan trades or does not. Display only: the plan decides with the same numbers in `planner`.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Options
from .prices import PriceSlot
from .simulate import Battery
from .tariffs import tariff


def _slots(o: Options, kind: str, price_slots: list[PriceSlot], now: datetime, tz: ZoneInfo,
           market_avg: float | None, hours: int) -> list[tuple[datetime, float, float, float | None]]:
    """(start, buy, sell, market) of each price period in the coming hours, in c/kWh."""
    t = tariff(kind, o)
    end = now + timedelta(hours=hours)
    out = []
    if kind == "dynamic":
        for p in sorted(price_slots, key=lambda p: p.start):
            if p.end > now and p.start < end:
                local = max(p.start, now).astimezone(tz)
                out.append((max(p.start, now), t.buy(local, p.eur_mwh), t.sell(local, p.eur_mwh, market_avg), p.eur_mwh))
    else:
        h = now.replace(minute=0, second=0, microsecond=0)
        while h < end:
            local = max(h, now).astimezone(tz)
            out.append((max(h, now), t.buy(local, None), t.sell(local, None, market_avg), None))
            h += timedelta(hours=1)
    return out


def margins(o: Options, kind: str, price_slots: list[PriceSlot], now: datetime, tz: ZoneInfo,
            market_avg: float | None = None, hours: int = 24) -> dict | None:
    """The best trade and the best shift in the coming hours, per kWh bought from the grid.

    trade: buy a kWh, sell what is left of it later. shift: buy a kWh, use what is left of it in a
    later hour instead of buying then. `margin_c` is what one bought kWh earns after the conversion
    losses and battery wear (negative: it loses). None without prices for this contract."""
    slots = _slots(o, kind, price_slots, now, tz, market_avg, hours)
    if len(slots) < 2:
        return None
    b = Battery.from_options(o)
    eta = b.eta_c * b.eta_d
    wear = b.wear_c_kwh * b.eta_c            # wear is counted per kWh that leaves the battery cells
    best: dict[str, tuple | None] = {"trade": None, "shift": None}
    low = slots[0]                           # the cheapest purchase so far
    for s in slots[1:]:
        for name, price in (("trade", s[2]), ("shift", s[1])):
            m = price * eta - wear - low[1]
            if best[name] is None or m > best[name][0]:
                best[name] = (m, low, s, price)
        if s[1] < low[1]:
            low = s
    out: dict = {"round_trip_pct": round(eta * 100), "wear_c_kwh": b.wear_c_kwh}
    for name, (m, buy, later, price) in best.items():
        out[name] = {"margin_c": round(m, 2), "buy_at": buy[0].isoformat(), "buy_c": round(buy[1], 2),
                     "at": later[0].isoformat(), "price_c": round(price, 2)}
    # the sell price (and market price) from which the best purchase would break even
    buy = best["trade"][1]
    need = (buy[1] + wear) / eta
    out["trade"]["needed_sell_c"] = round(need, 2)
    if kind == "dynamic" and o.dynamic_sell_mult > 0:
        out["trade"]["needed_market_eur_mwh"] = round(
            (need + o.injection_fee_c_kwh - o.dynamic_sell_add) / o.dynamic_sell_mult)
    return out
