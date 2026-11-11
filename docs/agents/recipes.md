# Recipes

Step lists for the jobs that come back. Each ends with `python tools/check.py`.

## Add a setting

1. `battery_pilot/pilot/config.py`: a field on `Options` with its default; a rule in `validate()`
   when a value can be wrong.
2. `battery_pilot/config.yaml`: the same key under `options` (same default) and under `schema`,
   with its type and range: `int(0,50)`, `float(0,100)`, `bool`, `str`, `list(a|b)`; a `?` after
   the type (`str?`) makes it optional and it then has no entry under `options`.
3. `battery_pilot/translations/en.yaml`: name and description.
4. `battery_pilot/DOCS.md`: a row in the table of the group it belongs to under "All settings"
   (Contract, Grid and capacity tariff, Battery, Planning, ...).
5. Use it through `self.o.<name>` (or `Battery.from_options` when the simulator needs it).
6. If it changes what the battery does, it must act in `planner.optimize`, `simulate.step` and
   `controller.decide` alike, with a test for each.

`tests/test_packaging.py` fails when one of steps 1-4 is missing or the defaults differ. The
settings export and import pick a new option up by themselves (`settings.KNOWN` is built from
`Options`). A release note goes in the CHANGELOG as for any change.
Choose a default that leaves existing behaviour unchanged: `Options()` is used all over the tests
and a default that changes the plan breaks many of them for no reason (see "Change default rates").

## Add an entity the app reads or writes

1. `battery_pilot/pilot/discovery.py`: an entry in `ENTITIES` (domain, name patterns in order,
   required, is a power sensor) and, when optional, a line in `NEEDED_FOR`.
2. `battery_pilot/pilot/config.py`: the key in `ENTITY_KEYS`; `config.yaml` gets it as `str?` in
   the schema only (no default), `translations/en.yaml` and `DOCS.md` a line each.
3. `tests/fake_ha.py`: the entity in `FakeHA`, behind a constructor flag when it is optional.

An entity that must never be guessed (like the car charger) gets an empty pattern list.

## Add a helper switch

`battery_pilot/pilot/helpers.py`: a constant and an entry in `HELPERS`. The entity id must be the
slug of the name, because Home Assistant derives it. Read it in `App.tick`, add it to
`tests/fake_ha.py` (`with_helpers`) and to the text about the switches in `battery_pilot/DOCS.md`
and in the root `README.md`.

## Change the page

The page is one file, `battery_pilot/pilot/static/index.html`: no build, no framework. Data comes
from `App.status()` through `GET /api/status` every 30 s.

1. A new card gets `data-tab="<tab>"`; a new tab is an entry in `TABS` in the script.
2. Charts take their width from the visible card: `showTab` renders again, keep it that way.
3. Check it: `bash tools/page.sh --shots <folder>` (about 35 s; a folder outside the repository)
   and look at the screenshots. It starts the quick demo, runs `tools/pagecheck.cjs` and stops the
   demo; run `node tools/pagecheck.cjs [url]` by itself only against a demo you already started.
   Needs Node and Playwright. The project skill `.claude/skills/verify-page/SKILL.md` is this recipe.
4. When the look changed, regenerate the docs screenshots (below). `DOCS.md` links them on
   `main`, so users see new ones only after `main` moves.

## Regenerate the screenshots

    python tools/demo_dashboard.py          # a year of history: about 5 minutes, wait for "Serving on"
    node tools/screenshots.cjs              # writes docs/screenshots/*.png

The quick demo (`--quick`) has too little history for the comparison and shade screenshots.

## Answer "why does the plan do this?"

`python tools/plan_probe.py` prints the planner's steps for a situation. Start there before
reading `planner.py`:

    python tools/plan_probe.py --at 2026-11-11T19:00 --soc 30 --solar 3 --load 0.45 --peak-shave
    python tools/plan_probe.py --settings exported.json --soc 23 --peak-shave      # the owner's settings
    python tools/plan_probe.py --at 2026-06-14T17:00 --soc 90 --solar 30 --contract dynamic \
        --prices "base=80,19-21=300"

`--settings` takes the file from the page's *Settings* tab (ask the owner to export it; never
commit it). `--peak-shave` and `--solar-hold` say the inverter has those switches; without
`--peak-shave` the planner never plans peak shaving. Usual causes, most common first:

1. the peak buffer is off (`peak_buffer_pct` is 0 by default), so the battery runs to its minimum;
2. the battery is at its minimum, where self-use and peak shaving do the same and the plan shows
   self-use;
3. the peak-shaving switch was not found (the startup log says so);
4. the winter rule is on (little solar tomorrow): no export, and a night target only when a night
   charge pays at the configured rates, which it does not with the default day and night rates.

The page's *Planned vs actual* tab shows what the plan expected against what happened, per day.

## Change default rates (a new tariff card)

The defaults are a Flemish Eneco contract on Fluvius Antwerpen. From a supplier's card:

- dynamic: `(dynamic_buy_mult × Belpex + dynamic_buy_add) × (1 + VAT)` and
  `dynamic_sell_mult × Belpex + dynamic_sell_add`, the yearly fee;
- `grid_fees_c_kwh` = distribution offtake tariff + excise + green power levy, for the grid area;
- `capacity_tariff_eur_kw_year` for the same area.

Change `config.py`, `config.yaml` and the "All settings" tables in `DOCS.md` together. Then:

- tests that state the defaults (`tests/test_basics.py`, the break-evens in `tests/test_app.py`,
  `tests/test_trading.py`) get the new numbers;
- tests that check what the plan does at a price level keep `fake_ha.RATES_EARLY_2026`: do not
  retune planner behaviour tests to new rates.

An update never changes options a user already saved, so say in the CHANGELOG that existing
installs keep their values.

## Release

A release is a version bump plus a CHANGELOG entry; Home Assistant offers the update when `main`
has the new version.

1. `python tools/release.py X.Y.Z` (patch for a fix, minor for a feature).
2. Write the entry in `battery_pilot/CHANGELOG.md`: what changes for the user, in their words.
3. `battery_pilot/DOCS.md` when users see the change; `CLAUDE.md` or `docs/agents/` when the
   code's shape changed.
4. `python tools/check.py` (the full run).
5. Commit (the tool does not) and push the work branch.
6. Only when the owner asked in this conversation, move `main`:

       git fetch origin main && git merge-base --is-ancestor origin/main HEAD && git push origin HEAD:main
       git ls-remote origin refs/heads/main        # must be the commit you pushed

   Never forced. See `docs/agents/workflow.md`.

Not every commit needs a release: tools, tests and docs for developers change nothing for users
and get no version. The project skill `.claude/skills/release/SKILL.md` is this recipe.
