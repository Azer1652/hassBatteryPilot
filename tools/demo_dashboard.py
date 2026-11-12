"""Serve the Battery Pilot dashboard with demo data, for screenshots or a local preview.

Nothing here talks to a real Home Assistant. The history (12 months of comparison,
monthly peaks, the learned tree shade and consumption) comes from the synthetic year
in tools/yearsim.py, replayed with the app's own code. Then the real app runs for the
last three days against the simulated house, battery and Solis from tests/fake_ha.py
with control switched on, so the plan, the planned-vs-actual days, readings, inverter
commands and events on the page are what the app actually did. The panels deliver a
little more or less than the forecast from day to day, as real weather does.

    python tools/demo_dashboard.py              # http://127.0.0.1:8765/ (Ctrl+C to stop)
    python tools/demo_dashboard.py --days 60    # quicker, less history
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "battery_pilot"))
sys.path.insert(0, os.path.join(HERE, "..", "tests"))
sys.path.insert(0, HERE)

import yearsim  # noqa: E402
from aiohttp import web  # noqa: E402
from fake_ha import TZ, FakeHA  # noqa: E402

from pilot.app import App  # noqa: E402
from pilot.config import Options  # noqa: E402
from pilot.web import make_web  # noqa: E402

TODAY = date(2026, 11, 9)          # a Monday in November: a dull day, tree shade, the battery kept for the peaks
NOW = datetime.combine(TODAY, time(23, 0), TZ)
LIVE_DAYS = 3                      # days the real app runs before NOW (the planned-vs-actual history)


def version() -> str:
    """The app version in config.yaml, so the demo header never lags behind."""
    with open(os.path.join(HERE, "..", "battery_pilot", "config.yaml"), encoding="utf-8") as f:
        return next(line.split('"')[1] for line in f if line.startswith("version:"))


def history(days: int, o: Options) -> dict:
    """The synthetic year up to the days the app runs itself, replayed per contract (a few minutes for a year)."""
    print(f"Replaying {days} synthetic days for the history...", flush=True)
    return yearsim.run(TODAY - timedelta(days=days), days - LIVE_DAYS + 1, o, peak_shave=True)


def fill(app: App, res: dict) -> None:
    app.shade, app.load = res["shade"], res["load"]
    app.pv.shade = app.shade
    for key, m in res["months"].items():
        acc = app.comparison.month(key)
        acc.update(imp_day=m["same"]["imp_day"], imp_night=m["same"]["imp_night"], exp=m["same"]["exp"],
                   market_sum=sum(m["market"]), market_n=len(m["market"]), dyn_c=m["same"]["dyn_c"],
                   peak_kw=m["fixed"]["peak_kw"], replay_days=m["days"])
        for kind in ("fixed", "dynamic"):
            acc["replay"][kind].update({k: m[kind][k] for k in ("cost_c", "imp", "exp", "charged", "discharged",
                                                                 "grid_charged", "peak_kw")})
        app.peaks.months[key] = round(m["fixed"]["peak_kw"], 2)
        for i, avg in enumerate(m["market"]):
            app.daily_market[(date.fromisoformat(key + "-01") + timedelta(days=i)).isoformat()] = round(avg, 2)


async def main(days: int, port: int, seconds: float | None):
    o = Options(car_min_kw=3.5, car_battery_kwh=60, car_charge_kw=5.0)      # (shows the car estimate)
    res = history(days, o)
    w = yearsim.World(seed=1)
    live = [TODAY + timedelta(days=i) for i in range(-LIVE_DAYS, 2)]
    fc = {h: v for d in live for h, v in w.forecast(d).items()}
    prices = [{"time": p.start.isoformat(), "price": round(p.eur_mwh / 1000, 5)}
              for d in live for p in w.prices(d)]
    weather = {d: random.Random(d.toordinal()).uniform(0.75, 1.2) for d in live}   # forecast error per day

    def pv(t):
        local = t.astimezone(TZ)
        hour = t.replace(minute=0, second=0, microsecond=0)
        return fc.get(hour, 0.0) * yearsim.true_shade(local.month, local.hour) * weather[local.date()]

    start = datetime.combine(TODAY - timedelta(days=LIVE_DAYS), time(16, 0), TZ).astimezone(timezone.utc)
    fake = FakeHA(start, soc=38, load=lambda t: w.load_kw(t.astimezone(TZ)), pv=pv, forecast=fc, prices=prices,
                  peak_shaving=True)
    fake.s["input_boolean.battery_pilot_control"]["state"] = "on"
    data = tempfile.mkdtemp(prefix="battery-pilot-demo-")
    options_path = os.path.join(data, "options.json")     # (the settings card exports and imports this file)
    with open(options_path, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in vars(o).items() if k != "entities"}, f)
    app = App(o, fake, data_dir=data, clock=lambda: fake.now, version=f"{version()} · demo data",
              options_path=options_path)
    await app.setup()
    fill(app, res)
    print(f"Running the app for {LIVE_DAYS} days...", flush=True)
    while fake.now < NOW.astimezone(timezone.utc) + timedelta(minutes=2):
        await app.step()
        fake.advance(timedelta(seconds=30))
    await app.publish()

    runner = web.AppRunner(make_web(app, None), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    print(f"Serving on http://127.0.0.1:{port}/", flush=True)
    if seconds:
        await asyncio.sleep(seconds)
    else:
        await asyncio.Event().wait()
    await runner.cleanup()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365,
                    help="days of synthetic history (a year takes about 5 minutes; the screenshots need it)")
    ap.add_argument("--quick", action="store_true",
                    help="3 days of history: ready in about 20 s, enough to look at or check the page")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--seconds", type=float, default=None, help="stop after this many seconds")
    a = ap.parse_args()
    asyncio.run(main(3 if a.quick else a.days, a.port, a.seconds))
