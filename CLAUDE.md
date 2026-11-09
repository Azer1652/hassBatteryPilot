# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Home Assistant app (add-on) repository (`repository.yaml` at the root) with one app,
`battery_playbook/`. The app runs once (`startup: once`): it reads its options, finds the
user's Solis / Forecast.Solar / ENTSO-e entities through the Home Assistant API, renders
three YAML package templates into Home Assistant's packages folder, asks Home Assistant
to check the configuration, and restarts it. All the battery logic (automations,
template sensors, helpers) lives in the templates and runs inside Home Assistant, not in
this app. The repository must hold no personal data: user entities and rates come only
from the app's options.

There are no tests, no linter config and no CI. The image is built on the user's
Home Assistant from `battery_playbook/Dockerfile` (base image from `BUILD_FROM`, then
`apk add python3` only).

## Commands

Render all three templates with the fallback entity IDs and check that they parse
(PyYAML is only for this check; it is not in the image):

```bash
BP_TEMPLATES=battery_playbook/templates python3 -c '
import sys, yaml; sys.path.insert(0, "battery_playbook"); import run
v = {k: e[4] for k, e in run.ENTITIES.items()}
v.update({k + "_scale": "0.001" for k, e in run.ENTITIES.items() if e[3]})
v.update(notify_service="notify.notify", import_limit=2.3, night_margin=30, window_start="22:00:00",
         window_end="07:00:00", grid_fees=11.96, capacity=52.37, fixed_day=18.59, fixed_night=15.22)
for p in run.PACKAGES: yaml.safe_load(run.render(p, v)); print(p, "ok")'
```

`render()` raises `Stop` if any `@@placeholder@@` is left unfilled. A valid YAML parse
does not mean Home Assistant accepts the package; only `check_config` on a real
instance proves that.

To run `run.py` outside Home Assistant, point its environment variables at local
paths and a stub API: `BP_OPTIONS` (options JSON, default `/data/options.json`),
`BP_TEMPLATES`, `BP_CONFIG_DIR` (folder containing `configuration.yaml`), `BP_BACKUP`,
`BP_API` (default `http://supervisor/core/api`), `SUPERVISOR_TOKEN`. The stub must answer
`GET /`, `GET /states`, `GET /services`, `POST /config/core/check_config` and
`POST /services/homeassistant/restart`. `run.py` writes to the config folder, so use a
temporary copy.

## Architecture

`battery_playbook/run.py` (stdlib only: the image has no pip packages) does, in order:

1. `packages_dir()`: finds `packages: !include_dir_named <dir>` in `configuration.yaml`,
   or inserts it under `homeassistant:` when `auto_configure_packages` is on.
2. `manual_copies()`: stops if a YAML file in the packages folder that lacks the
   generated-file header `MARK` contains a package's `SIGNATURES` helper (a hand-copied
   package would clash).
3. Entity resolution: each key in `ENTITIES` is `(domain, regex patterns, required,
   is_power, fallback)`. An option value overrides the search; otherwise patterns are
   tried in order against the object ID, ties are broken by a single `solis` match, and
   slot entities are narrowed by `slot_entities` (`time_charging` vs `grid_time_of_use`).
   Power sensors also get a `<key>_scale` value (`1` for kW, `0.001` for W or no unit).
   Missing required entities stop the run; missing optional ones fall back to the
   placeholder ID and are logged with the reason from `NEEDED_BY`.
4. `render()`: plain string replacement of `@@key@@` in `templates/<package>.yaml`, then
   prepends the `MARK` header. Lines a user would otherwise edit carry the comment
   `# set by the Battery Playbook app`.
5. Writes only changed files, records `(path, old content)` in `changes`, runs
   `check_config`, and on failure calls `rollback(changes)`. On success it saves the
   previous versions to `BP_BACKUP` and restarts Home Assistant.

Every abort goes through `raise Stop(message)`: the message is what the user reads in the
app log, so it should say which setting to change. Call `rollback(changes)` before
raising once anything has been written.

### Package dependencies

- `peak_shaving.yaml` owns `script.ps_inverter`, the only place that writes to the
  inverter (commands: window, resume, current, charge_now, discharge_now, self_use), and
  the `ps_*` helpers and sensors.
- `dynamic_tariff.yaml` (`dyn_*`) calls `script.ps_inverter` and reads many `ps_*`
  sensors; `contract_watch.yaml` (`cw_*`) reads `sensor.ps_import_quarter`. So `run.py`
  skips both when peak shaving is not installed. Renaming a `ps_*` entity means
  updating the other templates too.
- Helper prefixes (`ps_`, `dyn_`, `cw_`) and the `SIGNATURES` in `run.py` must stay in sync.

## Changing settings and entities

One option touches several files, which must agree:

- `battery_playbook/config.yaml`: `options` (default) and `schema` (type and range;
  entity overrides are `str?`).
- `battery_playbook/translations/en.yaml`: name and description for every schema key
  (currently an exact match with the schema).
- `run.py`: an entity goes in `ENTITIES` (and `NEEDED_BY` if optional); a plain setting
  goes in the `values.update({...})` mapping in `main()`, which renames option keys to
  placeholder names (e.g. `import_limit_kw` → `@@import_limit@@`).
- The template that uses the `@@placeholder@@`.
- `DOCS.md` settings table (user guide shown in Home Assistant's Documentation tab).

For a release, bump `version` in `config.yaml` (that is what Home Assistant offers as an
update; it reaches `run.py` as `BP_VERSION` via `BUILD_VERSION`) and add a
`CHANGELOG.md` entry. User-facing text is plain English for non-technical users.
