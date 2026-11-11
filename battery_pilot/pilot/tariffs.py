"""Contract price models, in c/kWh.

Both contracts pay the same grid fees and taxes on every bought kWh and the
same injection fee on every sold kWh; only the energy part differs.

Fixed:   buy  = day or night rate + grid fees
         sell = fixed_sell_mult x (this month's average market price, EUR/MWh) + fixed_sell_add - injection fee
Dynamic: buy  = (dynamic_buy_mult x market + dynamic_buy_add) x (1 + VAT) + grid fees
         sell = dynamic_sell_mult x market + dynamic_sell_add - injection fee
"""
from __future__ import annotations

from datetime import datetime, time

from .config import Options


def is_night(t_local: datetime, opts: Options) -> bool:
    """Night tariff (and night charging window) at a local time."""
    if opts.weekend_is_night and t_local.weekday() >= 5:
        return True
    return in_window(t_local.time(), opts.night_start_time, opts.night_end_time)


def in_window(t: time, start: time, end: time) -> bool:
    if start < end:
        return start <= t < end
    return t >= start or t < end


class Tariff:
    kind = ""

    def __init__(self, opts: Options):
        self.o = opts

    @property
    def yearly_fee_eur(self) -> float:
        raise NotImplementedError

    def buy(self, t_local: datetime, market: float | None) -> float:
        raise NotImplementedError

    def sell(self, t_local: datetime, market: float | None, market_avg: float | None) -> float:
        raise NotImplementedError


class FixedTariff(Tariff):
    kind = "fixed"

    @property
    def yearly_fee_eur(self) -> float:
        return self.o.fixed_yearly_fee_eur

    def buy(self, t_local, market=None):
        rate = self.o.fixed_night_rate if is_night(t_local, self.o) else self.o.fixed_day_rate
        return rate + self.o.grid_fees_c_kwh

    def sell(self, t_local, market=None, market_avg=None):
        avg = market_avg if market_avg is not None else (market or 0.0)
        return self.o.fixed_sell_mult * avg + self.o.fixed_sell_add - self.o.injection_fee_c_kwh


class DynamicTariff(Tariff):
    kind = "dynamic"

    @property
    def yearly_fee_eur(self) -> float:
        return self.o.dynamic_yearly_fee_eur

    def buy(self, t_local, market):
        m = market or 0.0
        return ((self.o.dynamic_buy_mult * m + self.o.dynamic_buy_add) * (1 + self.o.dynamic_vat_pct / 100)
                + self.o.grid_fees_c_kwh)

    def sell(self, t_local, market, market_avg=None):
        m = market or 0.0
        return self.o.dynamic_sell_mult * m + self.o.dynamic_sell_add - self.o.injection_fee_c_kwh


def tariff(kind: str, opts: Options) -> Tariff:
    return FixedTariff(opts) if kind == "fixed" else DynamicTariff(opts)


def breakeven_eur_mwh(fixed_rate: float, opts: Options) -> float | None:
    """Market price (EUR/MWh) below which a dynamic kWh is cheaper than this fixed rate.
    None when the dynamic price does not follow the market (multiplier 0): there is none."""
    if opts.dynamic_buy_mult <= 0:
        return None
    vat = 1 + opts.dynamic_vat_pct / 100
    return (fixed_rate / vat - opts.dynamic_buy_add) / opts.dynamic_buy_mult


def capacity_month_eur(peak_kw: float, opts: Options) -> float:
    """Capacity tariff for one month at this monthly peak (0 when the tariff is off)."""
    if opts.capacity_tariff_eur_kw_year <= 0:
        return 0.0
    return max(peak_kw, opts.capacity_min_kw) * opts.capacity_tariff_eur_kw_year / 12
