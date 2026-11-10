# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Home Assistant app (add-on) repository (`repository.yaml`) with one app, `battery_pilot/`
(Battery Pilot, formerly Battery Playbook). It is an always-running Python service: it reads HA
every 10 s, plans a home battery against contract prices, solar forecast and the capacity tariff,
steers a Solis hybrid through the Solis Modbus integration, compares fixed and dynamic contracts,
and serves a dashboard panel through ingress (port 8099). It is Solis-only by design.
The repository must hold no personal data: entities and rates come only from the app's options;
the defaults are a Flemish Eneco/Fluvius setup.

Runtime is Python 3 + `aiohttp` from Alpine packages (`battery_pilot/Dockerfile`, base images
in `build.yaml`). Do not add pip dependencies to the image.

## Commands

```bash
pip install -r requirements-dev.txt          # aiohttp, pytest, pytest-asyncio, pytest-aiohttp, pyyaml
pytest -q                                    # whole suite (~5 s)
pytest tests/test_planner.py -k winter       # one test or group
ruff check .                                 # lint (config in ruff.toml)
docker build battery_pilot -t battery-pilot --build-arg BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.21
```

Run the service locally against a real HA: `cd battery_pilot && BP_API=http://ha:8123/api
BP_WS=ws://ha:8123/api/websocket SUPERVISOR_TOKEN=<long-lived token> BP_OPTIONS=options.json
BP_DATA=/tmp/bp BP_ALLOW_ANY_IP=1 python3 -m pilot`. **Careful:** it will drive the inverter if
the `input_boolean.battery_pilot_control` helper is on.

CI (`.github/workflows/ci.yaml`): ruff + pytest on 3.12/3.13, `frenck/action-addon-linter`, image
build + import smoke test.

## Architecture (`battery_pilot/pilot/`)

Data flow: `app.App.step()` every 10 s → `tick()` reads entities → `meter.QuarterMeter` integrates
power into quarter-hour `QuarterRecord`s → peaks, shadow bill, stored records. Every 5 min
`refresh()` reads prices (`prices.parse_state`) and the solar forecast (websocket
`energy/solar_forecast`, else daily totals spread by `solar.hourly_from_daily`), then `replan()`.
`controller.decide()` turns the current plan step + live readings into one `solis.Solis` command.
Once a night `nightly()` learns shade and load from yesterday's records and replays yesterday.

- `planner.optimize()`: dynamic programme over SOC in 1 % levels. Actions are only what a Solis can
  execute: self-use, grid charge (at least `MIN_CHARGE_KW`, at most import limit −
  `CHARGE_HEADROOM_KW`), discharge/export (not below reserve). Costs: buy − sell, wear, penalty above
  the import limit, `ACTION_COST_C_PER_H`; terminal value for energy left. `make_plan()` adds the
  winter rule: when the next day's corrected solar < threshold, a soft target (`night_target()`) at
  the end of the night window, and no export. Two rules give way to money: in `extreme_slots()`
  (selling beats buying back at the dearest later price) export is allowed in winter and down to the
  minimum (`Step.reserve`); `_boost()` re-plans with higher import limits and keeps one only when it
  beats the normal plan after the capacity cost, priced once per month above
  max(month peak so far, `capacity_min_kw`) (`Step.limit_kw`, `Plan.boost`). Controller, simulator
  and replay follow those step fields. These constants were tuned against observed
  behaviour (see the planner tests); changing them changes behaviour on fixed contracts.
- `simulate.step()` is the shared energy-balance model (plan replay, night target, tests).
  `controller` must stay consistent with it: charging is paced to the step's SOC, follows house load
  in 5 A steps, pauses when the quarter-hour projection exceeds the limit.
- `solis.py` sets slot 1 to the current clock hour only (charge or discharge), so commands expire by
  themselves if the app stops; `controller` renews them each hour.
- `compare.Comparison`: per-month "same usage" shadow bill + nightly `replay_day()` per contract
  (plans hourly with forecasts, simulates with measured load/PV, includes wear). Fees and capacity
  tariff are added in `report()`. The replay, like the live plan, blocks export at negative sell
  prices only when `App.blocks_negative_export` (block switch on and an export-limit entity found).
- `solar.ShadeModel`: 12 × 24 actual/forecast factors weighted by forecast kWh; skips hours with
  incomplete PV data. `load.LoadProfile`: 7 × 24 kW EMA.
- `migrate.py`: takes over from Battery Playbook 1.x (turns its automations off, imports
  `sensor.cw_shadow_bill`, removes `battery_playbook_*.yaml` packages after a config check).
- `helpers.py`: creates the two `input_boolean` helpers over the websocket (no YAML).
- `history.py`: planned vs actual. `App._log_plan()` keeps per day (`day_logs`) the day-ahead plan
  (the replan at the end of `nightly()`) and the plan at the start of each clock hour; `nightly()`
  closes yesterday into `/data/history/YYYY-MM-DD.json` (60 days) with the measured quarters
  (`QuarterRecord.mode` = inverter mode). Served by `/api/history` and `/api/history/{day}`.
- State lives in `/data/state.json` (`App.save_state`/`load_state`).

## Tests

`tests/fake_ha.py` is an in-memory HA with a house/battery/Solis physics model (`advance()`);
`tests/test_app.py` runs the whole app through simulated nights with it. Planner/controller tests
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

## Changing settings

A new option touches: `config.Options` (field + default), `config.yaml` (`options` and `schema`),
`translations/en.yaml`, and `DOCS.md` if user-facing. Entity overrides are `str?` in the schema and
listed in `config.ENTITY_KEYS` (option name via `OVERRIDE_OPTION`) and `discovery.ENTITIES`.
For a release, bump `version` in `config.yaml` and add a `CHANGELOG.md` entry.
