"""Known bugs from the code review of 2026-11-17, one test each (numbered as in that report).

Every test here states the behaviour that is wanted and FAILS ON PURPOSE until its bug is fixed.
When a fix lands, its test passes: move it into the module it belongs to (test_planner, test_controller,
test_force, test_app, ...) and drop it from this file. Nothing here was checked on a real inverter
(docs/agents/unverified.md).
"""
import asyncio
import os
import re
import threading
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from fake_ha import CHEAP_NIGHT, TZ, FakeHA, utc
from test_car import day_of_quarters

from pilot import app as app_module
from pilot import settings
from pilot.app import App
from pilot.compare import Comparison
from pilot.config import ConfigError, Options
from pilot.controller import Readings, decide, fill_step
from pilot.meter import PeakTracker, QuarterMeter, QuarterRecord
from pilot.planner import Step, make_plan, slot_grid
from pilot.prices import PriceSlot, parse_state
from pilot.simulate import Battery
from pilot.simulate import step as sim_step
from pilot.tariffs import DynamicTariff, FixedTariff

CONTROL = "input_boolean.battery_pilot_control"
CURTAIL = "input_boolean.battery_pilot_block_negative_export"
SHAVE = "switch.solis_peak_shaving_mode"
TOU = "switch.solis_time_of_use"
EXPORT_LIMIT = "number.solis_export_power_limit"
PRICE = "sensor.current_electricity_market_price"
CONFIG_YAML = os.path.join(os.path.dirname(__file__), "..", "battery_pilot", "config.yaml")


async def run_until(app, fake, end, step_s=10, each=None):
    while fake.now < end:
        try:
            await app.step()
        except Exception as e:  # noqa: BLE001 - what App.run does with an error out of a round
            app._error(e)
        if each:
            each(app, fake)
        fake.advance(timedelta(seconds=step_s))


def energy(kw_at):
    """kWh between two UTC times for a load given in kW as a function of time (15-minute steps)."""
    def kwh(a, z):
        total, t = 0.0, a
        while t < z:
            t2 = min(t + timedelta(minutes=15), z)
            total += kw_at(t) * (t2 - t).total_seconds() / 3600
            t = t2
        return total
    return kwh


# ---------------------------------------------------------------- 1. boosted plan vs simulator
def _boost_plan(spike: float):
    """14 March, 10:00: an evening at 3.4 kW (price 150) before a dear morning (400, 3.0 kW), battery at 40 %."""
    o = Options(contract="dynamic")
    start = datetime(2026, 3, 14, tzinfo=TZ).astimezone(timezone.utc)
    prices = []
    for h in range(72):
        t = start + timedelta(hours=h)
        loc = t.astimezone(TZ)
        p = spike if (loc.day == 14 and loc.hour == 13) else 150.0 if 18 <= loc.hour < 21 \
            else 400.0 if 7 <= loc.hour < 9 else 100.0
        prices.append(PriceSlot(t, t + timedelta(hours=1), p))

    def kw_at(t):
        h = t.astimezone(TZ).hour
        return 3.4 if 18 <= h < 21 else 3.0 if 7 <= h < 9 else 0.45
    now = datetime(2026, 3, 14, 10, 0, tzinfo=TZ).astimezone(timezone.utc)
    plan = make_plan(now, TZ, o, DynamicTariff(o), prices, energy(kw_at), lambda a, z: 0.0, soc=40,
                     peak_kw=lambda a, z: max(kw_at(a), kw_at(z - timedelta(seconds=1))), peak_shave=True)
    return o, plan


def _drift(o, plan) -> list[tuple[str, str, float]]:
    """Replay the plan's steps through the simulator with their own fields: (time, action, SOC simulated - planned)."""
    b, soc, out = Battery.from_options(o), plan.steps[0].soc_start, []
    for s in plan.steps:
        soc = sim_step(b, soc, s.action, s.soc_end, s.load_kwh, s.pv_kwh, s.hours, limit_kw=s.limit_kw,
                       reserve=s.reserve, hold=s.hold).soc
        out.append((f"{s.start.astimezone(TZ):%d %H:%M}", s.action, round(soc - s.soc_end, 1)))
    return out


def test_bug01_a_boosted_plan_agrees_with_the_simulator():
    """A boost re-plans the whole horizon with the higher import limit, but only its charge steps get
    `limit_kw`: its peak-shaving steps leave up to the boost level to the grid, while the inverter
    (Solis.peak_shave at o.import_limit_kw) and the replay (simulate.step) shave at the normal limit."""
    o, normal = _boost_plan(100.0)                                       # the same days without the spike: no boost
    assert not normal.boost and all(abs(d[2]) <= 3 for d in _drift(o, normal)), "precondition: 3 points is a fair margin"
    o, plan = _boost_plan(-8000.0)
    assert plan.boost, "precondition: this scenario plans a boost"
    drift = _drift(o, plan)
    first = next((d for d in drift if abs(d[2]) > 3), None)
    worst = min(drift, key=lambda d: d[2])
    assert first is None, (f"the battery leaves the boosted plan at {first[0]} ({first[1]}, {first[2]} SOC points) and "
                           f"ends up to {worst[2]} points under it: planned at the boost limit "
                           f"{plan.boost['limit_kw']} kW, shaved at {o.import_limit_kw} kW")


# ---------------------------------------------------------------- 2. read-back without a time-of-use switch
@pytest.mark.parametrize("tou", ["unavailable", "not found"])
async def test_bug02_the_peak_shaving_switch_is_read_back_without_a_time_of_use_switch(tmp_path, tou):
    """App._reconcile returns early when the time-of-use switch has no state, so the peak-shaving (and feed-in)
    switches are not read back either; in force mode the time-of-use switch is optional."""
    fake = FakeHA(utc(2026, 6, 14, 14, 0), soc=60, peak_shaving=True, force_action=True, pv=lambda t: 3.0)
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(), fake, data_dir=str(tmp_path / "d"), clock=lambda: fake.now)
    assert await app.setup() and app.solis.method == "force"
    if tou == "unavailable":
        fake.override[TOU] = "unavailable"
    else:                                   # force mode without the entity: discovery leaves it out
        app.ent_ids.pop("tou_switch")
        app.solis.entities.pop("tou_switch")
    await run_until(app, fake, fake.now + timedelta(minutes=2))
    wanted = fake.s[SHAVE]["state"]
    fake.s[SHAVE]["state"] = "off" if wanted == "on" else "on"      # changed by hand or by the Solis app
    await run_until(app, fake, fake.now + timedelta(minutes=15))
    assert fake.s[SHAVE]["state"] == wanted, (f"time-of-use switch {tou}: the peak-shaving switch that Battery Pilot "
                                              f"set {wanted!r} stayed {fake.s[SHAVE]['state']!r} for 15 minutes")


# ---------------------------------------------------------------- 3. winter level, night window inside one day
def test_bug03_the_winter_level_is_kept_all_day_with_a_night_window_inside_one_day():
    """make_plan lets the level go from `night_start` on a dull day before a sunny one; with a window that does not
    cross midnight (01:00-06:00) that is 01:00 of the dull day itself, so the whole day loses its level."""
    def pv_kw(t):
        loc = t.astimezone(TZ)
        return (3.0 if loc.day == 14 else 30.0) / 7 if 9 <= loc.hour < 16 else 0.0   # dull 14 Jan, sunny 15 Jan
    o = Options(night_start="01:00", night_end="06:00", winter_hold_soc=40, weekend_is_night=False)
    now = datetime(2026, 1, 14, 0, 30, tzinfo=TZ).astimezone(timezone.utc)
    plan = make_plan(now, TZ, o, FixedTariff(o), [], energy(lambda t: 0.5), energy(pv_kw), 60, peak_shave=True,
                     forecast_until=datetime(2026, 1, 16, 16, 0, tzinfo=TZ).astimezone(timezone.utc))
    dull = [s for s in plan.steps if (s.start + timedelta(hours=s.hours / 2)).astimezone(TZ).day == 14]
    lost = [f"{s.start.astimezone(TZ):%H:%M}" for s in dull if s.hold != 40]
    assert not lost, f"the dull day loses its winter level from {lost[0]} ({len(lost)} of {len(dull)} steps)"


# ---------------------------------------------------------------- 4. an unknown inverter state taken for a charge
def test_bug04_an_unknown_inverter_state_is_not_taken_for_a_charge():
    """`failed` (after a failed command or a Home Assistant restart) is treated like `charge` in the peak-buffer
    branch of decide() and in fill_step: the battery is then charged from the grid."""
    o = Options(battery_min_soc=10, peak_buffer_pct=20)                  # buffer level 30 %
    t = datetime(2026, 1, 15, 19, 10, tzinfo=TZ).astimezone(timezone.utc)
    step = Step(t.replace(minute=0), 1.0, "self_use", 29, 28, 0, 0, 0.5, 0, 25, 5, 0)
    r = Readings(t, t.astimezone(TZ), 29.0, 0.6, 0.0, 0.6, 0.6, 51.2)  # in the band: under the level, above level - 3

    def cmd(mode):
        return decide(step, r, o, mode, t.astimezone(TZ).strftime("%H:00:00"), 0, True, can_shave=True).command
    assert cmd("failed") == cmd("self_use") == "peak_shave", f"mode 'failed' in the buffer band gives {cmd('failed')!r}"
    full = Readings(t, t.astimezone(TZ), 97.0, 0.6, 0.0, 0.6, 0.6, 51.2)
    assert fill_step(full, "failed", True).action == "peak_shave", "fill switch at 97 %: 'failed' starts a grid charge"


async def test_bug04_a_home_assistant_restart_in_the_peak_buffer_band_buys_nothing(tmp_path):
    fake = FakeHA(utc(2026, 1, 14, 19, 0), soc=28.6, peak_shaving=True, load=lambda t: 0.6)
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(battery_min_soc=10, peak_buffer_pct=20), fake, data_dir=str(tmp_path / "d"), clock=lambda: fake.now)
    assert await app.setup()
    await run_until(app, fake, fake.now + timedelta(minutes=3))
    assert fake.mode() == "peak_shave", "precondition: the buffer is kept by peak shaving"
    soc0 = fake.soc
    fake.away = True                                                     # Home Assistant restarts for 2 minutes
    await run_until(app, fake, fake.now + timedelta(minutes=2))
    fake.away = False
    charging = []
    await run_until(app, fake, fake.now + timedelta(minutes=30),
                    each=lambda a, f: charging.append(f.now) if f.mode() == "charge" else None)
    assert not charging and fake.soc <= soc0 + 0.2, (f"after the restart the battery was grid-charged for "
                                                     f"{len(charging) * 10} s, SOC {soc0:.1f} -> {fake.soc:.1f}")


# ---------------------------------------------------------------- 5. export limit put back
async def test_bug05_lifting_the_export_block_puts_back_the_inverters_own_limit(tmp_path):
    """App._curtail restores min(the limit it found, export_limit_kw) instead of the limit it found."""
    day = datetime(2026, 6, 14, tzinfo=TZ)
    prices = [{"time": (day + timedelta(hours=h)).isoformat(), "price": -0.05 if 12 <= h < 13 else 0.08} for h in range(48)]
    fake = FakeHA(utc(2026, 6, 14, 11, 50), soc=60, prices=prices, pv=lambda t: 4.0)
    fake.s[EXPORT_LIMIT]["state"] = "8000"                               # the installation's own export limit
    fake.s[CONTROL]["state"] = "on"
    fake.s[CURTAIL]["state"] = "on"
    app = App(Options(contract="dynamic", export_limit_kw=5.0), fake, data_dir=str(tmp_path / "d"), clock=lambda: fake.now)
    assert await app.setup()
    await run_until(app, fake, utc(2026, 6, 14, 12, 10))
    assert fake.s[EXPORT_LIMIT]["state"] == "0", "precondition: export is blocked in the negative hour"
    await run_until(app, fake, utc(2026, 6, 14, 13, 10))
    assert fake.s[EXPORT_LIMIT]["state"] == "8000", f"the 8000 W export limit was put back as {fake.s[EXPORT_LIMIT]['state']} W"


# ---------------------------------------------------------------- 6. frozen SOC on a big battery
async def test_bug06_a_big_battery_charging_slowly_is_not_taken_for_a_frozen_soc(tmp_path):
    """FROZEN_S (30 min) does not scale with the battery: 48 kWh at 15 A moves one whole SOC point in about 40 min."""
    fake = FakeHA(utc(2026, 1, 15, 1, 0), soc=40)
    o = Options(battery_capacity_kwh=48.0)
    app = App(o, fake, data_dir=str(tmp_path / "d"), clock=lambda: fake.now)
    assert await app.setup()
    app.solis.mode, app.solis.amps = "charge", 15                        # a paced charge at 15 A
    dc_kw = 15 * 51.2 / 1000 * o.charge_efficiency
    t, soc, frozen_at = fake.now, 40.0, None
    for _ in range(6 * 120):                                             # two hours of 10-second ticks
        soc += dc_kw * 10 / 3600 / (o.battery_capacity_kwh / 100)
        if app._frozen(t, float(int(soc))) and frozen_at is None:        # the Solis reports whole points
            frozen_at = (t - fake.now).total_seconds() / 60
        t += timedelta(seconds=10)
    assert frozen_at is None, f"a battery that charges normally was declared frozen after {frozen_at:.0f} minutes"


# ---------------------------------------------------------------- 7. the last minute before midnight
async def test_bug07_a_night_charge_goes_on_through_the_last_minute_before_midnight(tmp_path):
    """solis.hour_slot ends the 23:00 slot at 23:59:00: from 23:59 to 00:00 the inverter is back in self-use.
    (The slot end is exclusive in tests/fake_ha.py; on a real Solis this is not confirmed.)"""
    def dull(day):
        return {datetime(day.year, day.month, day.day, h, tzinfo=TZ).astimezone(timezone.utc): 2 / 7 for h in range(9, 16)}
    fake = FakeHA(utc(2026, 1, 14, 22, 50), soc=11, load=lambda t: 0.4,
                  forecast={**dull(datetime(2026, 1, 14)), **dull(datetime(2026, 1, 15))})
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(**CHEAP_NIGHT, safety_margin_pct=60), fake, data_dir=str(tmp_path / "d"), clock=lambda: fake.now)
    assert await app.setup()
    seen = {}

    def watch(a, f):
        loc = f.now.astimezone(TZ)
        if (loc.hour, loc.minute) >= (23, 57) or (loc.hour, loc.minute) < (0, 2):
            seen[f"{loc:%H:%M:%S}"] = f.mode()
    await run_until(app, fake, utc(2026, 1, 15, 0, 2), each=watch)
    assert seen.get("23:58:00") == "charge", "precondition: charging before midnight"
    gap = sorted(t for t, m in seen.items() if m != "charge")
    assert not gap, f"not charging from {gap[0]} to {gap[-1]} ({len(gap) * 10} s)"


# ---------------------------------------------------------------- 8. the control loop during the nightly replay
async def test_bug08_the_control_loop_keeps_running_during_the_nightly_replay(tmp_path, monkeypatch):
    """App.step awaits nightly(), which awaits the replay in an executor: no tick runs until it is done
    (1-3 s on a PC, longer on a Raspberry Pi), at 00:20, in the night-charge window."""
    fake = FakeHA(utc(2026, 1, 15, 0, 19, 30), soc=40)
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(), fake, data_dir=str(tmp_path / "d"), clock=lambda: fake.now)
    assert await app.setup()
    app.records = day_of_quarters()                                      # yesterday, 14 Jan: something to replay
    started, release = threading.Event(), threading.Event()

    def busy_replay(*args, **kwargs):
        started.set()
        release.wait(5)
        return None
    app._replay = busy_replay
    during = []
    real_tick = app.tick

    async def tick():
        during.append(started.is_set() and not release.is_set())
        await real_tick()
    app.tick = tick

    async def sleep(event, seconds):                                     # fake time passes, the loop goes round quickly
        fake.advance(timedelta(seconds=seconds))
        await asyncio.sleep(0.01)
    monkeypatch.setattr(app_module, "_sleep", sleep)
    stop = asyncio.Event()
    task = asyncio.create_task(app.run(stop))
    for _ in range(500):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set(), "precondition: the nightly replay started"
    await asyncio.sleep(1.0)                                             # about 100 rounds of the loop if it were free
    release.set()
    stop.set()
    await asyncio.wait_for(task, 10)
    assert sum(during) >= 3, f"{sum(during)} ticks ran while the nightly replay was busy for 1 s"


# ---------------------------------------------------------------- 9. slots at the autumn clock change
@pytest.mark.parametrize("now,minutes", [(datetime(2026, 10, 25, 0, 50, tzinfo=timezone.utc), 15),
                                         (datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc), 60)])
def test_bug09_no_long_first_slot_in_the_first_hour_of_the_autumn_clock_change(now, minutes):
    """slot_grid adds timedelta to a local time (wall-clock arithmetic): in the first 02:xx (CEST) the first
    slot runs to 03:00 CET, 70 instead of 10 minutes (quarter-hours) or 90 instead of 30 (hours)."""
    grid = slot_grid(now, now + timedelta(hours=3), TZ, minutes)
    longest = max((b - a).total_seconds() / 60 for a, b in grid)
    assert longest <= minutes, f"a slot of {longest:.0f} minutes at {now.astimezone(TZ):%H:%M %Z} ({minutes}-minute grid)"
    inner = [b for _, b in grid[:-1]]
    assert all(b.minute % minutes == 0 and b.second == 0 for b in inner)


# ---------------------------------------------------------------- 10. settings outside the add-on schema
def test_bug10_a_settings_import_outside_the_add_on_schema_is_refused():
    """settings.prepare checks types but not the ranges in config.yaml: the check says fine, then the
    Supervisor refuses the save. An empty optional text (notify_service) is also passed on."""
    with open(CONFIG_YAML, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    current = dict(cfg["options"])
    accepted = []
    for key, rule in cfg["schema"].items():
        m = re.fullmatch(r"(int|float)\(([-\d.]+),([-\d.]+)\)\??", str(rule))
        if not m:
            continue
        kind, lo, hi = m.group(1), float(m.group(2)), float(m.group(3))
        outside = (int(lo) - 1, int(hi) + 1) if kind == "int" else (round(lo - 0.1, 3), round(hi + 0.1, 3))
        for value in outside:
            try:
                settings.prepare({key: value}, current)
            except ConfigError:
                continue
            accepted.append(f"{key}={value} ({rule})")
    assert not accepted, f"{len(accepted)} out-of-range values accepted: {', '.join(accepted)}"
    merged, _, _ = settings.prepare({"notify_service": ""}, current)
    assert "notify_service" not in merged, "an empty notify_service is passed on to the Supervisor"


# ---------------------------------------------------------------- 11. peaks of partly measured quarters
def test_bug11_a_partly_measured_quarter_counts_at_its_measured_power():
    """PeakTracker.add and Comparison.add_quarter take kWh x 4 without scaling by the share of the quarter measured."""
    t = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc)
    rec = QuarterRecord(t, {"import": 0.6, "export": 0.0, "load": 0.6, "pv": 0.0},       # 4 kW for 60 % of the quarter
                        {"import": 0.6, "export": 0.6, "load": 0.6, "pv": 0.6})
    peaks, cmp = PeakTracker(), Comparison()
    peaks.add(rec, TZ)
    cmp.add_quarter(rec, None, Options(), TZ)
    assert peaks.month_peak(t, TZ) == pytest.approx(4.0), f"peak tracker: {peaks.month_peak(t, TZ)} kW"
    assert cmp.months["2026-01"]["peak_kw"] == pytest.approx(4.0), f"comparison: {cmp.months['2026-01']['peak_kw']} kW"


# ---------------------------------------------------------------- 12. naive price times on the clock-change day
def test_bug12_naive_local_price_times_keep_both_hours_of_the_autumn_clock_change():
    """parse_time gives both 02:00 the same UTC time (fold 0): the second overwrites the first, a 1-hour gap."""
    walls = [f"2026-10-25T{h:02d}:00:00" for h in (0, 1, 2, 2, *range(3, 24))]
    st = {"state": "0.1", "attributes": {"unit_of_measurement": "EUR/kWh",
                                         "prices": [{"time": w, "price": 0.05 + i / 1000} for i, w in enumerate(walls)]}}
    slots = parse_state(st, TZ)
    gaps = [f"{a.end:%H:%M}-{b.start:%H:%M} UTC" for a, b in zip(slots, slots[1:]) if b.start != a.end]
    assert len(slots) == 25 and not gaps, f"{len(slots)} slots for a 25-hour day, gaps: {gaps}"


# ---------------------------------------------------------------- 13. the wall clock stepping back
def test_bug13_a_clock_that_steps_back_does_not_count_energy_twice():
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


# ---------------------------------------------------------------- 14. a price unit that is guessed
async def test_bug14_a_price_sensor_without_a_unit_is_reported_once(tmp_path):
    """prices.unit_factor guesses the unit from the size of the numbers when the sensor has none; 25 c/kWh and
    25 EUR/MWh look the same, so the user must be told what was assumed."""
    day = datetime(2026, 1, 14, tzinfo=TZ)
    prices = [{"time": (day + timedelta(hours=h)).isoformat(), "price": 18.0 + (h % 24) / 2} for h in range(48)]

    async def events(unit):
        fake = FakeHA(utc(2026, 1, 14, 12, 0), prices=prices)
        if unit is None:
            del fake.s[PRICE]["attributes"]["unit_of_measurement"]
        else:
            fake.s[PRICE]["attributes"]["unit_of_measurement"] = unit
        app = App(Options(contract="dynamic"), fake, data_dir=str(tmp_path / str(unit)), clock=lambda: fake.now)
        assert await app.setup()
        for _ in range(2):
            fake.advance(timedelta(minutes=5))
            await app.refresh()
        return [e["text"] for e in app.events if PRICE in e["text"] and "unit" in e["text"]]
    assert await events("c/kWh") == []
    told = await events(None)
    assert len(told) == 1, f"no unit on {PRICE}: {len(told)} events say which unit was assumed"
