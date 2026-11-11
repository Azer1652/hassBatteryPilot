# Architecture

How Battery Pilot is put together, for someone about to change it. Paths are under
`battery_pilot/pilot/` unless they start with `tests/` or `tools/`. The user-facing reference is
`battery_pilot/DOCS.md`; this file is about the code.

## Data flow

`app.App.step()` every 10 s → `tick()` reads entities → `meter.QuarterMeter` integrates
power into quarter-hour `QuarterRecord`s → peaks, shadow bill, stored records. Every 5 min
`refresh()` reads prices (`prices.parse_state`; ENTSO-e has the list on its *average* sensor, so a
`current_electricity_market_price` without a list is followed to it, and a sensor without a list is logged once) and the solar forecast (websocket
`energy/solar_forecast`, else daily totals spread by `solar.hourly_from_daily`), then `replan()`.
`controller.decide()` turns the current plan step + live readings into one `solis.Solis` command.
Once a night `nightly()` learns shade and load from yesterday's records and replays yesterday.

## Module map

| File | What it holds |
| --- | --- |
| `__main__.py` | entry point: reads the options, builds `HA` and `App`, serves the page |
| `app.py` | `App`: the 10 s loop, refresh every 5 min, the nightly learning and replay, state, status for the page |
| `planner.py` | the dynamic programme and `make_plan` (winter rule, extreme slots, boost) |
| `simulate.py` | `Battery` and `step`: the energy balance shared by plan replay, night target and tests |
| `controller.py` | `decide`: one plan step + live readings → one inverter command |
| `solis.py` | `Solis`: the commands, through the Solis Modbus integration's entities |
| `discovery.py` | finds the user's entities by name (`ENTITIES`), unless the options name them |
| `config.py` | `Options` (every setting with its default), validation, `from_dict` |
| `tariffs.py` | buy and sell price per contract, break-even |
| `prices.py` | price lists from ENTSO-e, Nord Pool or a generic sensor → `PriceSlot` |
| `solar.py` | `PvForecast` (shade, intraday correction) and `ShadeModel` |
| `load.py` | `LoadProfile`: 7 × 24 house consumption |
| `meter.py` | `QuarterMeter`, `QuarterRecord`, `PeakTracker` |
| `compare.py` | `Comparison` (shadow bill per month) and `replay_day` |
| `car.py` | car charging detection (`CarState`, sources) |
| `efficiency.py` | measured round-trip efficiency (display only) |
| `trading.py` | best trade margins of the next 24 h (display only) |
| `history.py` | planned vs actual per day |
| `helpers.py` | the three `input_boolean` helpers |
| `settings.py` | export and import of the options |
| `migrate.py` | takes over from Battery Playbook 1.x |
| `ha.py` | REST and websocket client; `supervisor()` for the app's own options |
| `web.py` | the ingress page and its JSON routes |
| `static/index.html` | the whole page: one file, no build step |

## The planner (`planner.py`)

`planner.optimize()`: dynamic programme over SOC in 1 % levels. Actions are only what a Solis can
execute: self-use, grid charge (at least `MIN_CHARGE_KW`, at most import limit −
`CHARGE_HEADROOM_KW`), discharge/export (not below reserve), and `peak_shave` when the inverter's
peak-shaving switch was found (`App.can_peak_shave`, passed as `peak_shave=` to `make_plan`,
`optimize` and `replay_day`): the grid feeds the house up to the import limit, the battery gives
only what is above it (`_shave_kwh`). It has no action cost and no switch cost against self-use,
and is chosen over self-use only when it also wins on a second valuation (`low`) in which energy
left at the end is worth what the cheapest hour saves: with the normal terminal value alone the
battery was held all night before sunny days, which cost money in `yearsim`. Costs: buy − sell, wear, penalty above
the import limit, `ACTION_COST_C_PER_H`; terminal value for energy left. Charge and discharge move
whole SOC points; self-use moves a fraction of one per slot, so its rounding error is carried to the
next slot (`whole`), it is costed with the flows of the exact change (`_flow`) and the value of the
next slot is interpolated. Rounding each slot by itself made the plan drift from the battery, badly
on 15-minute slots. `make_plan()` adds the
winter rule: when the next day's corrected solar < threshold, a soft target (`night_target()`) at
the end of the night window, and no export. The target is set only when `night_charge_pays()`
(cheapest night buy / eta_c < mean day buy × eta_d − wear): with the default day/night rates it
does not, and a forced refill also made the plan hold the battery with a day-rate trickle charge
in the evening. Tests that need a winter night charge use `fake_ha.CHEAP_NIGHT`. Two rules give way to money: in `extreme_slots()`
(selling beats buying back at the dearest later price) export is allowed in winter and down to the
minimum (`Step.reserve`); `_boost()` re-plans with higher import limits and keeps one only when it
beats the normal plan after the capacity cost, priced once per month above
max(month peak so far, `capacity_min_kw`) (`Step.limit_kw`, `Plan.boost`). Controller, simulator
and replay follow those step fields. These constants were tuned against observed
behaviour (see the planner tests); changing them changes behaviour on fixed contracts.

Steadiness: `optimize(first_action=...)` charges `SWITCH_COST_C` for changing what the previous
plan is still doing now (`planner.sticky_action`; a finished step is not sticky).
`planner.step_to_follow()` is the step `App.tick` hands to the controller: the minutes left before
a slot boundary are a first slot too short for one SOC point, which the optimizer can only make
self-use (equal prices are merged, different ones are not); a running charge or discharge that the
next step continues carries on through it. It never starts one.

## The simulator and the controller (`simulate.py`, `controller.py`)

`simulate.step()` is the shared energy-balance model (plan replay, night target, tests).
`controller` must stay consistent with it: charging is paced to the step's SOC, follows house load
in 5 A steps, pauses when the quarter-hour projection exceeds the limit.

## The Solis and the safety net (`solis.py`, `App.tick`)

`solis.py` sets slot 1 to the current clock hour only (charge or discharge); `controller` renews
them each hour. The Solis repeats slots daily, so `App` records what it left on the inverter
(`/data/inverter.json`) and returns it to self-use at the next start; `Solis.mode` is `failed`
after a failed command (treated as possibly active). `Solis.peak_shave()` turns time of use off,
writes the limit and the baseline SOC (battery minimum; only when changed) and turns the
peak-shaving switch on; every other command turns it off (`_shave_off`). `peak_shave` is in
`controller.ACTIVE` with charge, discharge and failed: control off, blind, stop and restart
return to self-use, and `_reconcile` reads that switch back too. `tick()` stops a charge/discharge after
`BLIND_S` without SOC/import (or a frozen SOC), keeps helper states through short read failures,
and reads the time-of-use switch back (`_reconcile`). `refresh()` runs as a background task.
Health: the Dockerfile `HEALTHCHECK` calls `/health` (503 when the main loop stalls,
`App.healthy`); the add-on linter rejects the old `watchdog` key in `config.yaml`.

## Solar hold and the peak buffer

`solar_hold` (the inverter's feed-in priority switch, `feed_in_priority_switch`, found by name): the
surplus is exported and not stored, so the battery keeps its room for hours where surplus cannot be
sold (negative sell price with export blocked, or above the export limit). No action cost; chosen only
where it lowers the cost. `controller.decide` switches it on with at least 0.3 kW surplus and returns
to self-use when it is gone (not within 120 s of the last command), since the battery may not feed the
house then. Like `peak_shave` it is in `controller.ACTIVE`, ended on control off, blind, stop and
restart, and read back by `_reconcile`. Unverified on a real inverter. Without the entity nothing changes.

The peak buffer (`peak_buffer_pct`, off by default; `Battery.buffer_soc`/`self_floor`): self-use stops at
minimum + buffer and below it the battery gives only what is above the import limit. `simulate.step`
does that in self-use; `optimize` (`self_move`, only with `peak_shave=True`) the same and labels such a
step `peak_shave`; `controller.decide` switches to peak shaving at the level (leaves it `BUFFER_BAND`
above) and charges back to it from `BUFFER_GAP` below. That refill is not in the planner or the replay.
`App.setup` zeroes the option when the peak-shaving switch was not found.

## The car (`car.py`) and the fill switch

`car.py`: `CarState` (charging, power_kw, and `soc`/`plugged`/`target_soc`, None for now) from a source
chosen in `car.make_source`: `ChargerPowerSource` (`car_charger_power` set, never auto-found; above 1 kW),
`LoadStepSource` (`car_min_kw` > 0: jump over the 10-minute median for 3 min), else `NoCar`. A car
integration would be a new source filling `soc`/`plugged`. `App._car` feeds `controller.decide(car_kw=,
can_shave=)`: while the car charges above the import limit it forces `peak_shave`. `QuarterRecord.car`
is kept out of `LoadProfile` and the peaks; the replay uses peak_shave in car quarters; the plan adds
the car's power for the next hour. `status["car"]`, `sensor.battery_pilot_car_power`, `#carnote`.

The fill switch (`helpers.CAR_FILL`, third helper; the page's `#fillbtn` posts to `/api/car_fill` →
`App.set_fill`): while on and the car is not charging, `controller.decide(fill=True)`
replaces the plan's step by `fill_step` (charge to 100 % in the current clock hour; once full
`peak_shave` when the inverter has it, charging again below `FILL_AGAIN`). The plan is not changed.
`App._fill` turns the helper off when a car session ends or after `FILL_MAX_H` (`fill_since` is persisted).

## The contract comparison (`compare.py`)

`compare.Comparison`: per-month "same usage" shadow bill + nightly `replay_day()` per contract
(plans hourly with forecasts, simulates with measured load/PV, includes wear). Fees and capacity
tariff are added in `report()`. Both contracts must cover the same hours: a month with market prices
is priced on the quarters that have one (`p_imp_day`, `p_imp_night`, `p_exp`), and a day enters the
replay totals only when both contracts were replayed. `App.daily_market` (the fixed sell price
follows the month's average) is taken from whole days only: the price archive keeps 3 days, and
what is left of a day while it is dropped is its evening. The replay, like the live plan, blocks export at negative sell
prices only when `App.blocks_negative_export` (block switch on and an export-limit entity found).

## Solar forecast and shade (`solar.py`), house load (`load.py`)

`solar.PvForecast.observe()` (called in `App.replan` and, through `replay_day(on_replan=)`, in the
nightly replay and `yearsim`): today's measured solar over the last 3 hours against the forecast after
shade (`intraday_factor`; blocked, incomplete and export-limited quarters skipped, 0.5 kWh pull to 1,
clamp 0.3-1.6) scales the rest of today, fading with a 4-hour half-life. Tomorrow, the winter rule and
shade learning (raw archive) are untouched. `yearsim --forecast-error` makes the forecast wrong about
the day; `--no-nowcast` switches the correction off.

`solar.ShadeModel`: 12 × 24 actual/forecast factors weighted by forecast kWh; skips hours with
incomplete PV data and hours with a `QuarterRecord.blocked` quarter (export held at 0 by
`App._curtail`: the inverter throttles the panels; the replay uses the forecast there when it is
higher). `load.LoadProfile`: 7 × 24 kW EMA.

## Display-only figures (`efficiency.py`, `trading.py`)

`efficiency.EfficiencyMeter`: the battery's measured round-trip efficiency (battery AC flow = load +
export − import − pv against the SOC change, quarters with complete data and SOC 15-95 %, 60-day
half-life, a value after 150 SOC points each way). `learn_records` is called from `App.nightly()`.
Display only (`status["efficiency"]`, `sensor.battery_pilot_efficiency`): the plan keeps using
`round_trip_efficiency_pct`.

`trading.margins()`: display only (`status["trading"]`, the `#tradenote` line in the plan card): the
best buy-then-sell pair and the best buy-to-avoid-buying pair of the next 24 h per contract, per kWh
bought, with the same efficiencies and wear as the planner. Computed in `App.replan`.

## The page and the settings file (`static/index.html`, `web.py`, `settings.py`)

The page (`static/index.html`, one file, no build) shows one `[data-tab]` block at a time (`showTab`, tab
kept in `localStorage`, `#<tab>` in the URL selects one, `#all` shows every card: `tools/screenshots.cjs`
uses that). Charts take their width from the visible card, so `showTab` renders again.

Settings export/import (`settings.py`, pure; the page's *Settings* tab): `GET /api/settings` gives the saved
options file (`App.options_path`, `saved_options()`); `POST /api/settings/check` and `POST /api/settings`
run `settings.prepare` (known keys only, types as the Supervisor wants them, merged onto the saved options,
validated with `config.from_dict`, empty entity overrides left out). Saving is `App.save_options` →
`HA.supervisor("POST", "/addons/self/options")` (`hassio_api: true`); the running `Options` are not touched,
the user restarts the app. Unverified against a real Supervisor.

## Helpers, history, storage, migration

`helpers.py`: creates the three `input_boolean` helpers over the websocket (no YAML).

`history.py`: planned vs actual. `App._log_plan()` keeps per day (`day_logs`) the day-ahead plan
(the replan at the end of `nightly()`) and the plan at the start of each clock hour; `nightly()`
closes yesterday into `/data/history/YYYY-MM-DD.json` (60 days) with the measured quarters
(`QuarterRecord.mode` = inverter mode). Served by `/api/history` and `/api/history/{day}`.

State lives in `/data/state.json` (`App.save_state`/`load_state`, fsynced; a damaged file is kept as
`state.json.bad` and the app starts fresh).

`migrate.py`: takes over from Battery Playbook 1.x (turns its automations off, imports
`sensor.cw_shadow_bill`, removes `battery_playbook_*.yaml` packages after a config check).

## Tests and tools

`tests/fake_ha.py` is an in-memory HA with a house/battery/Solis physics model (`advance()`;
`override` shows an entity as unavailable or frozen, `fail` makes service calls raise);
`tests/test_app.py`, `test_e2e.py`, `test_safety.py`, `test_steady.py` and `test_robust.py` run
the whole app through simulated time with it. Planner/controller tests
include regressions for the old playbook bugs (overcharge above target, night drain, shade collapse).
`tools/yearsim.py` is a synthetic year (shade, winter dip, peaks, seasonal and negative prices) replayed
with the app's own `replay_day`; `python tools/yearsim.py [--days N --winter-mode peaks_only]` prints a
monthly cost table, and `tests/test_scenarios.py` runs whole weeks of it.
`battery_pilot/DOCS.md` is the user reference (every setting, every entity read and written, every
published sensor); `test_packaging.py` fails when a setting, Solis entity or `sensor.battery_pilot_*`
is missing from it. Screenshots in `docs/screenshots/` come from `python tools/demo_dashboard.py`
(demo data through the real app) + `node tools/screenshots.cjs`; DOCS.md links them by raw GitHub URL
on `main`, the root README by relative path.
`tests/test_packaging.py` checks that `config.yaml` options/schema, `translations/en.yaml`,
`config.Options` defaults and the CHANGELOG version agree — update all of them together.

Developer tools: `tools/check.py` (lint + tests), `tools/release.py` (version + CHANGELOG entry),
`tools/plan_probe.py` (print the plan for a described situation), `tools/pagecheck.cjs` and
`tools/page.sh` (the page in a real browser), `tools/demo_dashboard.py`, `tools/screenshots.cjs`,
`tools/yearsim.py`. See `docs/agents/recipes.md` for when to use which.
