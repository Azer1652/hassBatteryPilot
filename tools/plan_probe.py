"""Print the plan Battery Pilot would make for a situation you describe.

For "why does the plan do this?" questions: describe the moment (time, battery level, solar,
house load, prices), optionally with a settings file exported from the page's Settings tab, and
read the steps the planner chooses. It calls the app's own `planner.make_plan`.

    python tools/plan_probe.py                                   # defaults, a November evening
    python tools/plan_probe.py --at 2026-06-14T17:00 --soc 90 --solar 30 --contract dynamic \\
        --prices "base=80,19-21=300"
    python tools/plan_probe.py --settings my-settings.json --soc 23 --peak-shave

Times are local (`--tz`). `--prices` is the market price in EUR/MWh: `base=` for every hour and
`H-H=` for local clock hours (end exclusive), the same every day. `--solar` is kWh per day after
shade, as a bell between 08:00 and 17:00. `--load` is a flat house load in kW.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "battery_pilot"))

from pilot.config import Options, from_dict  # noqa: E402
from pilot.planner import make_plan  # noqa: E402
from pilot.prices import PriceSlot  # noqa: E402
from pilot.tariffs import tariff  # noqa: E402

SUN_FROM, SUN_TO = 8.0, 17.0


def parse_prices(text: str) -> tuple[float, list[tuple[int, int, float]]]:
    base, spans = 100.0, []
    for part in filter(None, (p.strip() for p in text.split(","))):
        key, _, value = part.partition("=")
        if key == "base":
            base = float(value)
        else:
            a, _, b = key.partition("-")
            spans.append((int(a), int(b or int(a) + 1), float(value)))
    return base, spans


def price_slots(start: datetime, hours: int, tz: ZoneInfo, text: str) -> list[PriceSlot]:
    base, spans = parse_prices(text)
    first = start.replace(minute=0, second=0, microsecond=0)
    out = []
    for i in range(hours + 1):
        t = first + timedelta(hours=i)
        h = t.astimezone(tz).hour
        price = next((v for a, b, v in spans if (a <= h < b) or (a > b and (h >= a or h < b))), base)
        out.append(PriceSlot(t, t + timedelta(hours=1), price))
    return out


def solar_fn(day_kwh: float, tz: ZoneInfo):
    """kWh between two times: `day_kwh` per day as a half-sine between SUN_FROM and SUN_TO."""
    def share(h: float) -> float:       # part of the day's solar delivered by local hour h
        x = min(max((h - SUN_FROM) / (SUN_TO - SUN_FROM), 0.0), 1.0)
        return (1 - math.cos(math.pi * x)) / 2

    def kwh(a: datetime, z: datetime) -> float:
        total, t = 0.0, a
        while t < z:                    # per local day, so a span over midnight works
            local = t.astimezone(tz)
            day_end = (local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).astimezone(timezone.utc)
            u = min(z, day_end)
            h0 = local.hour + local.minute / 60 + local.second / 3600
            h1 = h0 + (u - t).total_seconds() / 3600
            total += day_kwh * (share(h1) - share(h0))
            t = u
        return total
    return kwh


def load_options(path: str | None, overrides: dict) -> Options:
    raw = {}
    if path:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        raw = raw.get("options", raw) if isinstance(raw, dict) else {}
    return from_dict({**raw, **overrides})


def probe(a) -> str:
    tz = ZoneInfo(a.tz)
    o = load_options(a.settings, {"contract": a.contract} if a.contract else {})
    now = datetime.fromisoformat(a.at).replace(tzinfo=tz).astimezone(timezone.utc)
    pv = solar_fn(a.solar, tz)
    plan = make_plan(now, tz, o, tariff(o.contract, o), price_slots(now, a.hours, tz, a.prices),
                     lambda s, e: a.load * (e - s).total_seconds() / 3600, pv, a.soc,
                     horizon_h=a.hours, peak_shave=a.peak_shave, solar_hold=a.solar_hold)
    lines = [f"contract {o.contract}, battery {o.battery_capacity_kwh:g} kWh, min {o.battery_min_soc:g} %, "
             f"reserve {o.battery_reserve_soc:g} %, peak buffer {o.peak_buffer_pct} %, import limit {o.import_limit_kw:g} kW",
             f"winter rule: {'on' if plan.winter else 'off'}; export allowed: {plan.allow_export}; "
             f"boost: {plan.boost or 'none'}"]
    for t in plan.targets:       # the night target of the winter rule, and whether it was applied
        lines.append("night target: " + ", ".join(f"{k} {v}" for k, v in t.items()))
    lines += ["",
             f"{'local time':<12}{'h':>5}  {'action':<11}{'SOC':>9}{'load':>7}{'solar':>7}{'import':>8}{'export':>8}"
             f"{'buy c':>7}{'sell c':>7}"]
    for s in plan.steps:
        lines.append(f"{s.start.astimezone(tz):%a %H:%M}   {s.hours:>5.2f}  {s.action:<11}{s.soc_start:>4.0f}→{s.soc_end:<4.0f}"
                     f"{s.load_kwh:>7.2f}{s.pv_kwh:>7.2f}{s.import_kwh:>8.2f}{s.export_kwh:>8.2f}{s.buy:>7.1f}{s.sell:>7.1f}")
    lines.append("")
    lines.append(f"total: import {sum(s.import_kwh for s in plan.steps):.1f} kWh, export "
                 f"{sum(s.export_kwh for s in plan.steps):.1f} kWh, cost {sum(s.cost_c for s in plan.steps) / 100:.2f} EUR "
                 "(energy, wear and what the planner charges for peaks and switching)")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--at", default="2026-11-11T19:00", help="local start time (default: a November evening)")
    ap.add_argument("--tz", default="Europe/Brussels")
    ap.add_argument("--soc", type=float, default=30.0, help="battery level now, %%")
    ap.add_argument("--contract", choices=("fixed", "dynamic"), help="default: the one in the settings")
    ap.add_argument("--settings", help="settings file exported from the page (or a bare options object)")
    ap.add_argument("--solar", type=float, default=3.0, help="solar per day after shade, kWh")
    ap.add_argument("--load", type=float, default=0.45, help="flat house load, kW")
    ap.add_argument("--prices", default="base=100", help='market EUR/MWh, e.g. "base=80,19-21=300,2-5=20"')
    ap.add_argument("--hours", type=int, default=36)
    ap.add_argument("--peak-shave", action="store_true", help="the inverter has the peak-shaving switch")
    ap.add_argument("--solar-hold", action="store_true", help="the inverter has the feed-in priority switch")
    print(probe(ap.parse_args(argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
