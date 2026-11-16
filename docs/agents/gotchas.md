# Gotchas

Things that cost time before. Read once.

## Running things

- Use `python -m pytest`, not bare `pytest`: the bare command may be another Python.
- The whole suite takes about 100 s; `python tools/check.py --fast` skips the tests marked `slow`.
- `tools/demo_dashboard.py` without `--quick` replays a year first: about 5 minutes of silence,
  then "Serving on". Wait for that line; do not start a second one on the same port.
- Playwright and Chromium are expected to be installed already. Do not install them again.

## The test fakes (`tests/fake_ha.py`)

- `utc(y, m, d, h, mi)` takes **local** (Brussels) time and returns UTC.
- The house load is `FakeHA.load_fn` (a function of time), not `load`.
- At an exact hour boundary the inverter slot of the past hour has ended: check the mode a few
  minutes before or after, not at `:00`.
- `FakeHA(prices=...)` puts the list on `sensor.current_electricity_market_price`, the layout of
  old ENTSO-e versions. The real integration keeps it on `sensor.average_electricity_price`.
- `CHEAP_NIGHT` are options with which a winter night charge pays; with the default rates it
  does not, so tests that need a night charge pass it.
- `RATES_EARLY_2026` are the rates the planner behaviour tests were tuned on.

## The planner

- Its constants were tuned against observed behaviour; changing one changes plans on fixed
  contracts. Run `tests/test_scenarios.py` and `python tools/yearsim.py --days 28` before and after.
- A new mechanism must exist in `simulate.step`, `planner.optimize` and `controller.decide`
  alike, or the plan, the replay and the live behaviour drift apart.
- In `controller.decide` the order of the branches matters: a rule placed after the `peak_shave`
  branch is unreachable when the plan labels the step peak shaving.
- `Battery.from_options` is called everywhere without knowing which inverter switches exist, so
  an option that needs a switch is gated in `App.setup`, not in the default.
- A plan must start at `now` (`slot_grid`): a `Plan.step_at(now)` of None makes the controller
  go to self-use ("no plan"), and the winter level then waits 15 min to come back. Price a slot by
  its middle, not its start: the first slot can begin seconds before its price slot.

## Home Assistant and the Supervisor

- A helper's entity id is derived from its name. Name it so the slug is the id you want.
- An update never changes options the user saved. New defaults reach new installs only.
- The Supervisor checks option types strictly (a whole number for `int`, no text for a number)
  and wants optional `str?` options left out when empty: see `settings._typed`.
- The page runs inside the ingress iframe, also in the Companion app, where file downloads are
  unreliable. Offer the text as well.
- A restart of Home Assistant looks like this from the app: entities go `unavailable` while it
  shuts down, then the Supervisor proxy answers 502 for 20-60 s, then entities are `unavailable`
  again while integrations load. `HA` raises `HAUnreachable` (502/503/504, connection errors) for
  the 502 part; `App.tick` waits it out without commands (`_ha_away` / `_ha_back`). Catch
  `HAUnreachable` before a generic `except` where an ERROR or WARNING would be logged.
  `FakeHA.away = True` simulates it.
- The add-on linter in CI rejects unknown or deprecated keys in `config.yaml` (`watchdog`).

## The page

- Charts measure their container. Inside a hidden tab the width is 0, so they must be rendered
  again when the tab is shown.
- `localStorage` can throw (private window): always inside try/catch.
- `#all` in the URL shows every card; `tools/screenshots.cjs` relies on it.

## Outside this repository

- In a cloud session many hosts are blocked (a supplier's download site, for example). Ask the
  owner for the file instead of retrying. `pdftotext -layout` reads tariff cards well.
