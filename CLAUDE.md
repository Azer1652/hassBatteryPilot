# CLAUDE.md

Guidance for Claude Code (claude.ai/code) and other agents working in this repository.
Keep this file short; the detail is in `docs/agents/`.

## What this is

A Home Assistant app (add-on) repository (`repository.yaml`) with one app, `battery_pilot/`
(Battery Pilot, formerly Battery Playbook). It is an always-running Python service: it reads
Home Assistant every 10 s, plans a home battery against contract prices, solar forecast and the
capacity tariff, steers a Solis hybrid through the Solis Modbus integration, compares fixed and
dynamic contracts, and serves a page through ingress (port 8099). It is Solis-only by design.

Runtime is Python 3 + `aiohttp` from Alpine packages (`battery_pilot/Dockerfile`, base images in
`battery_pilot/build.yaml`).

## Hard rules

- **No personal data in the repository.** Entities and rates come only from the app's options;
  the defaults are a public Flemish Eneco/Fluvius setup. No exported settings, tariff cards or
  screenshots of a real installation.
- **No pip dependencies in the image.**
- **`main` is what users install.** It moves only by fast-forward, only when the owner asks in
  that conversation. No pull request unless asked. See `docs/agents/workflow.md`.
- **A release is a version bump plus a CHANGELOG entry** (`python tools/release.py X.Y.Z`).
- **Planner, simulator and controller must agree.** A mechanism added to one of
  `planner.optimize`, `simulate.step`, `controller.decide` belongs in all three.
- **Say what is unverified.** Most inverter behaviour is tested in a simulator only:
  `docs/agents/unverified.md`.

## Commands

```bash
pip install -r requirements-dev.txt ruff     # aiohttp, pytest, pytest-asyncio, pytest-aiohttp, pyyaml
python tools/check.py                        # ruff + the whole suite (about 100 s)
python tools/check.py --fast                 # ruff + the tests not marked slow (about 25 s)
python -m pytest tests/test_planner.py -k winter     # one test or group (use `python -m pytest`)
python tools/plan_probe.py --help            # print the plan for a situation you describe
bash tools/page.sh --shots <folder>          # the page in a real browser, every tab (about 35 s)
python tools/demo_dashboard.py --quick       # demo data on http://127.0.0.1:8765/ (ready in about 20 s)
python tools/yearsim.py --days 28            # a synthetic month replayed per contract (cost table)
python tools/release.py X.Y.Z                # version + CHANGELOG heading (does not commit)
docker build battery_pilot -t battery-pilot --build-arg BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.21
```

Run the service against a real Home Assistant: `cd battery_pilot && BP_API=http://ha:8123/api
BP_WS=ws://ha:8123/api/websocket SUPERVISOR_TOKEN=<long-lived token> BP_OPTIONS=options.json
BP_DATA=/tmp/bp BP_ALLOW_ANY_IP=1 python3 -m pilot` (`options.json`: a file you write with the keys
under `options` in `battery_pilot/config.yaml`). **Careful:** it drives the inverter when the
`input_boolean.battery_pilot_control` helper is on.

CI (`.github/workflows/ci.yaml`): ruff + pytest on Python 3.12 and 3.13, the add-on linter, an
image build and an import smoke test.

## Where to look

| You want to | Read |
| --- | --- |
| understand the code: data flow, module map, each mechanism | `docs/agents/architecture.md` |
| add a setting, entity, helper or tab; release; change default rates; answer "why this plan?" | `docs/agents/recipes.md` |
| avoid the traps (test fakes, planner tuning, Supervisor, the page) | `docs/agents/gotchas.md` |
| know what was never confirmed on a real inverter | `docs/agents/unverified.md` |
| handle branches, `main`, commits and what to tell the owner | `docs/agents/workflow.md` |
| see what users are told | `battery_pilot/DOCS.md`, `battery_pilot/CHANGELOG.md` |

## The shape in ten lines

`app.App` runs a 10 s loop: read entities, meter quarter-hours, follow the plan. Every 5 minutes
it reads prices and the solar forecast and calls `planner.make_plan` (a dynamic programme over
battery level). `controller.decide` turns the current plan step and live readings into one
`solis.Solis` command: self-use, charge, discharge, peak shaving or solar hold. Once a night it
learns shade and consumption and replays yesterday on both contracts (`compare.replay_day`),
which uses the same `simulate.step` as the tests. `tests/fake_ha.py` is an in-memory Home
Assistant with a house, battery and Solis; most tests run the whole app through simulated time
against it. The page is one static file fed by `GET /api/status`.

## Keeping this useful

When you learn something the next agent would need (a trap, a recipe, something confirmed on the
real system), put it in the matching file under `docs/agents/`. Two project skills wrap the
commonest jobs: `.claude/skills/release/SKILL.md` and `.claude/skills/verify-page/SKILL.md`. `tests/test_tools.py` fails when
these docs name a file that does not exist or when this file grows past 120 lines.
