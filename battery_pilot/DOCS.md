# Battery Pilot

Battery Pilot steers a home battery on a Solis hybrid inverter so it costs you as
little as possible, and tells you whether a fixed or a dynamic electricity contract
would be cheaper for your house. It runs all the time and has its own page in the
sidebar.

![The Battery Pilot page: the 36-hour plan with a winter night charged to its target](https://raw.githubusercontent.com/Azer1652/hassSolarBattery/main/docs/screenshots/plan.png)

*Demo data. Blue: hours the battery is grid-charged. Green: the battery level.*

What it does:

- **Plans the battery** for the next 36 hours from your contract's prices, your
  house's consumption pattern, and the solar forecast corrected for shade. Each
  quarter-hour or hour is self-use, grid charge, or discharge/export. Charging and
  exporting only happen when the price difference beats battery losses and wear.
- **Keeps your capacity peak low.** Grid charging stays under the import limit and
  follows the house load. Charging pauses when the quarter-hour heads over the limit,
  so the battery can cover a cooking peak.
- **Handles winter.** On a dull day ahead (corrected solar below the threshold), the
  battery is grid-charged during the night to carry the house through the day. In
  winter no battery energy is exported.
- **Learns your tree shade.** It compares what your panels deliver with the forecast
  for every month and hour of the day. Low winter sun behind trees gets its own
  correction; summer noon keeps its own.
- **Compares contracts** with your own measured energy, in two ways (see below).
- **Alerts you** on your phone: capacity peak warnings, a monthly verdict, reminders
  before your fixed price ends, and when the market crosses your break-even.

## Before you start

1. **Solis Modbus** (HACS) with a working local connection to the inverter.
2. A **solar forecast** in the Energy dashboard (Settings → Dashboards → Energy →
   Solar panels → forecast), for example **Forecast.Solar**. Leave its damping off:
   Battery Pilot learns the shade itself.
3. For dynamic prices and the comparison: **ENTSO-e** (HACS, area BE) or **Nord Pool**
   (HACS). Without one, Battery Pilot still plans on a fixed contract.
4. The **Home Assistant Companion app** on your phone, for alerts.

## Install

1. Settings → Apps (Add-ons) → store → ⋮ → **Repositories**, add
   `https://github.com/Azer1652/hassSolarBattery`.
2. Install **Battery Pilot**, check the **Configuration** tab, **Save**, **Start**.
3. Open **Battery Pilot** in the sidebar.

The log lists every entity it found and how. If a required entity is missing or
several match, it says which setting to fill in (Configuration → *Show unused optional
configuration options*).

### Shadow mode first

Battery Pilot starts in **shadow mode**: it plans, compares and learns, but does not
touch the inverter. Watch the plan for a few days. When you trust it, switch on
**Battery Pilot control** (Settings → Devices & services → Helpers).

### Coming from Battery Playbook

Battery Pilot takes over by itself on its first start. It switches off the old
automations so they can't drive the inverter, takes over your contract history, and
removes the old package files (a copy is kept in the app's storage). Then restart
Home Assistant once and uninstall the Battery Playbook app.

## How it decides

| Situation | What the battery does |
| --- | --- |
| Dull day ahead (winter), strategy `full_day` | Charged from the grid at night, at the cheapest moments, to the **night target** by the time the night tariff ends. The target is what the house needs until evening after the corrected solar, plus the safety margin, but never more than the night window can charge under the import limit. No battery export. |
| Dull day ahead (winter), strategy `peaks_only` | Only enough charge to cover your usual quarter-hour peaks (cooking, kettle) under the import limit, plus whatever the prices make worth it. |
| Any day | The battery keeps enough energy for the quarter-hour peaks Battery Pilot has learned for each weekday and hour, topping up shortly before if needed. |
| Sunny day ahead | No grid charging: the sun fills the battery. |
| Fixed day/night contract | Buying at night to use by day does not pay after losses and wear, so it only grid-charges for the winter target. |
| Dynamic contract | Also charges in cheap hours and exports in expensive ones, when the spread beats losses + wear. |
| Negative sell price | With **Battery Pilot block negative export** on and an export-limit entity, export is set to 0. |
| Quarter-hour heading over the limit | Charging pauses; the battery helps the house. You get a phone warning when a peak is coming. |

Charge and discharge commands only cover the current clock hour. If Battery Pilot or
Home Assistant stops, the inverter returns to self-use within the hour.

## The comparison

- **Same usage:** every quarter-hour, what you actually bought and sold is priced on
  both contracts. It is exact, but your battery behaved as it does on your current
  contract.
- **Best battery use:** every night, yesterday is replayed with your measured
  consumption and solar. For each contract the planner decides every hour as it would
  have on that contract (cheap-hour charging, export, selling surplus solar, the
  winter night target). A simulated battery follows its commands, and wear is
  included. This answers: what would each contract have cost me, with the battery
  used the best way for it?

Both include the contract fees and the capacity tariff. The sidebar page shows every
month; the sensor `sensor.battery_pilot_contract_comparison` holds the 12-month
difference (negative means dynamic is cheaper).

![Contract comparison per month (demo data)](https://raw.githubusercontent.com/Azer1652/hassSolarBattery/main/docs/screenshots/comparison.png)

The page also shows the shade Battery Pilot learned for every month and hour (dark =
the forecast is too high, e.g. low winter sun behind trees) and your monthly peaks:

![Learned shade and monthly peaks (demo data)](https://raw.githubusercontent.com/Azer1652/hassSolarBattery/main/docs/screenshots/shade-peaks.png)

## What Battery Pilot reads, and why

Battery Pilot finds these entities by itself. To pick a different one, fill in its
setting on the Configuration tab (*Show unused optional configuration options*). If a
**required** one is missing, or several entities match, Battery Pilot stops and the
log says which setting to fill in.

### Measurements (read every 10 seconds)

| Setting | Found automatically | Needed | Used for | Without it |
| --- | --- | --- | --- | --- |
| `grid_import_power` | a sensor named like `grid_import_power` or `import_power` | required | The quarter-hour meter: monthly capacity peak, the projection of where this quarter-hour is heading, peak warnings, pausing charging, and the "same usage" bill (kWh bought). | Battery Pilot does not start. |
| `house_load_power` | `household_load_power`, `house_load` or `load_power` | required | Grid-charge current that keeps import under the limit (it follows the house load), learning your consumption pattern and your usual quarter-hour peaks, the night target, and the nightly replay. | Battery Pilot does not start. |
| `battery_soc` | `battery_soc` | required | Planning from the battery's real state, stopping a charge or export at its target, the nightly replay's starting point. | Battery Pilot does not start. |
| `grid_export_power` | `grid_export_power` or `export_power` (not an export *limit*) | optional | kWh sold, in the "same usage" bill. | Sold kWh count as 0 in the comparison. |
| `pv_power` | `total_pv_power` or `pv_power` | optional | Learning tree shade (what the panels delivered vs the forecast), the charge current (solar adds room under the limit), the nightly replay. | Shade is not learned (the forecast is used as is); hours without solar data are skipped, never counted as dark. |
| `battery_voltage_sensor` | `battery_voltage` | optional | Turning amps into kW for the charge current and the plan. | The *Battery nominal voltage* setting is used. |

Power sensors can be in W or kW: the unit is read from the entity.

### Prices and forecasts (read every 5 minutes)

| Setting | Found automatically | Needed | Used for | Without it |
| --- | --- | --- | --- | --- |
| `price_sensor` | `current_electricity_market_price` (ENTSO-e), `nordpool…` or `electricity_price` | for a dynamic contract | The hourly or quarter-hour market prices in its attributes (ENTSO-e `prices`, Nord Pool `raw_today`/`raw_tomorrow`, or a generic list), EUR/kWh or EUR/MWh from its unit. They drive the dynamic plan, the dynamic side of the comparison, the fixed contract's sell price (it follows the month's market average), break-even alerts and blocking export at negative prices. | Planning still works on a fixed contract; the comparison and dynamic prices are not available. With a flat fixed sell rate, set *Fixed: sell price × market* to 0. |
| — (Energy dashboard) | the solar forecast set up in Settings → Dashboards → Energy, read over the websocket API | recommended | Hour-by-hour solar forecast for the plan, corrected for shade. | The two daily totals below are spread over the daylight hours instead. |
| `forecast_today`, `forecast_tomorrow` | Forecast.Solar's `energy_production_today` / `_tomorrow` | fallback | Only when the Energy dashboard has no solar forecast. | No solar in the plan: every day looks like winter. |
| `forecast_power_now` | Forecast.Solar's `power_production_now` | optional | Read once from the recorder on the first start, to learn shade from the last 10 days at once. | Shade is learned from the first day on instead. |

### Solis controls (found at start; changed as listed in the next section)

| Setting | Found automatically | Needed |
| --- | --- | --- |
| `tou_switch` | switch `time_of_use` | required |
| `grid_charge_switch` | switch `allow_grid…charge` / `grid_charg…` | required |
| `charge_current` | number `…charge_current` (not discharge) | required |
| `discharge_current` | number `…discharge_current` | required |
| `charge_start`, `charge_end` | time `…charge_start…slot_1`, `…charge_end…slot_1` | required |
| `discharge_start`, `discharge_end` | time `…discharge_start…slot_1`, `…discharge_end…slot_1` | required |
| `export_limit` | number `export_power_limit` / `export…limit` | optional: only for blocking export at negative prices |

Slot entities come in two sets on Solis Modbus (`time_charging` and
`grid_time_of_use`); the *Solis slot entities* setting picks which one. When several
entities match, the one with `solis` in its name wins.

### Everything else it reads

- **Home Assistant's configuration:** time zone, latitude and longitude (for day/night,
  sunrise and sunset).
- **Notify actions:** to find the Companion app phone for alerts (unless *Phone
  notification action* is set).
- **Its two switches:** *Battery Pilot control* and *Battery Pilot block negative
  export*, every 10 seconds.
- **The recorder**, once on the first start: 10 days of house load, solar and forecast
  power, to learn your consumption, peaks and shade straight away.
- **Battery Playbook**, once, if it was installed: its automations and the sensors
  `sensor.cw_shadow_bill`, `sensor.ps_daily_consumption_avg` and
  `sensor.ps_monthly_peak` (see *Coming from Battery Playbook*).

## What Battery Pilot changes

### On the inverter, only while *Battery Pilot control* is on

| Command | When | What it sets |
| --- | --- | --- |
| Charge | The plan says grid-charge in this slot and there is room under the import limit. | Charge slot 1 start/end = the current clock hour (e.g. 02:00–03:00; 23:00–23:59 in the last hour), discharge slot 1 = 00:00–00:00, *allow grid charging* on, charge current, *time of use* on. |
| Discharge (export) | The plan says export in this slot. | Discharge slot 1 = the current clock hour, charge slot 1 = 00:00–00:00, discharge current, *time of use* on. |
| Change current | While charging or exporting, when the right current changes by 10 A or more (house load, pacing). | Charge or discharge current only. |
| Renew | At every new clock hour while still charging or exporting. | The same as Charge / Discharge for the new hour. |
| Self-use | The plan says self-use, a target is reached, charging pauses for a peak, *Battery Pilot control* is switched off, or the app stops. | *Time of use* off. Everything else is left as it is. |
| Block export | *Battery Pilot block negative export* is on, an export-limit entity exists and the sell price is below zero. | Export limit 0 W, and back to *Normal export limit* when the price is positive again. Battery Pilot only ever undoes its own 0 W, so a zero-export installation keeps its own setting. |

How the charge current is chosen: enough to reach the plan's SOC by the end of the
hour, never more than keeps grid import under the import limit with the current house
load (in 5 A steps, never above *Maximum charge current*). Charging starts only with 15 A
of room, pauses below 10 A or when the quarter-hour heads 0.1 kW over the limit, and
holds at 10 A (at most 2 % above target) once the target is reached before the hour ends.
Exports never go below *Battery reserve for export*.

Because every command covers the current clock hour only, the inverter returns to
self-use by itself within the hour if Battery Pilot or Home Assistant stops.

### In Home Assistant

- **Two switches**, created as normal helpers on the first start (off):
  `input_boolean.battery_pilot_control` and
  `input_boolean.battery_pilot_block_negative_export`.
- **Its sensors**, listed below. They are updated every 30 seconds and come back
  within 30 seconds after a Home Assistant restart.
- **Notifications** in HA, and on your phone through the Companion app:
  - capacity peak warning: the quarter-hour heads more than 0.3 kW over the limit for
    2 minutes; at most once per quarter-hour;
  - monthly verdict: on the 1st at 09:00;
  - fixed price ending: 42, 14 and 3 days before;
  - market crossing your break-even: the 30-day average moves below your night rate or
    above your day rate.
- **Files:** none, except once when taking over from Battery Playbook. It removes the
  `battery_playbook_*.yaml` package files after a configuration check (a copy goes to
  the app's own storage), switches off the old automations, and reloads HA's YAML.
- **Its own storage** (`/data/state.json`): what it learned (shade, consumption, peaks),
  the comparison history and the last days of prices and measurements.

## All settings

The defaults are an Eneco contract on a Fluvius connection in Flanders (2026) with a
16 kWh battery on a Solis S6-EH1P; change them to your own. Prices are incl. VAT.

### Contract

| Setting | Key | Default | What it does |
| --- | --- | --- | --- |
| Your contract now | `contract` | fixed | Which contract's prices the battery is planned for. The comparison always shows both. |
| Fixed price ends on | `fixed_contract_end` | (empty) | Date (YYYY-MM-DD) for the reminders 6 weeks, 2 weeks and 3 days before. |
| Fixed day rate | `fixed_day_rate` | 18.59 c/kWh | Energy price on the fixed contract by day, without grid fees. |
| Fixed night rate | `fixed_night_rate` | 15.22 c/kWh | The same at night. Equal to the day rate for a single-rate contract. |
| Fixed contract fee | `fixed_yearly_fee_eur` | 65 €/year | Added in the comparison. |
| Fixed: sell price × market | `fixed_sell_mult` | 0.0798 | Fixed sell price = this × the month's average market price (EUR/MWh) + the next setting − injection fee. 0 for a flat sell rate. |
| Fixed: sell price + | `fixed_sell_add` | −2.72 c/kWh | See above; your flat sell rate if the setting above is 0. |
| Dynamic: buy price × market | `dynamic_buy_mult` | 0.102 | Dynamic buy price = (this × market + the next setting) × (1 + VAT) + grid fees. |
| Dynamic: buy price + | `dynamic_buy_add` | 1.0 c/kWh | Supplier margin, before VAT. |
| Dynamic: VAT | `dynamic_vat_pct` | 6 % | VAT on the dynamic energy price. |
| Dynamic: sell price × market | `dynamic_sell_mult` | 0.1 | Dynamic sell price = this × market + the next setting − injection fee. |
| Dynamic: sell price + | `dynamic_sell_add` | −1.188 c/kWh | Usually negative: the supplier's deduction. |
| Dynamic contract fee | `dynamic_yearly_fee_eur` | 100 €/year | Added in the comparison. |
| Grid fees and taxes | `grid_fees_c_kwh` | 11.96 c/kWh | Added to every bought kWh on both contracts. Update each January. |
| Injection fee | `injection_fee_c_kwh` | 0.19 c/kWh | Subtracted from every sold kWh on both contracts. |
| Night tariff starts | `night_start` | 22:00 | Night rate, and the window for winter night charging. |
| Night tariff ends | `night_end` | 07:00 | On winter days the battery reaches its night target by this time. |
| Weekend is night tariff | `weekend_is_night` | on | The night rate all weekend (Flanders). |

The contract formulas in full (market prices in EUR/MWh):

- fixed buy = day or night rate + grid fees
- fixed sell = *fixed: sell price × market* × the month's average market price +
  *fixed: sell price +* − injection fee
- dynamic buy = (*buy × market* × market price + *buy +*) × (1 + VAT) + grid fees
- dynamic sell = *sell × market* × market price + *sell +* − injection fee

### Grid and capacity tariff

| Setting | Key | Default | What it does |
| --- | --- | --- | --- |
| Grid import limit | `import_limit_kw` | 2.3 kW | The quarter-hour average import Battery Pilot aims to stay under. Grid charging is planned 0.3 kW under it and pauses above it; the battery keeps energy for your usual peaks above it; warnings start 0.3 kW over it. |
| Normal export limit | `export_limit_kw` | 6.0 kW | Highest export the plan counts on, and the value restored after a negative-price block. |
| Capacity tariff | `capacity_tariff_eur_kw_year` | 52.37 €/kW/year | What one kW of monthly peak costs; weighs peaks in the plan and adds the capacity cost in the comparison. 0 if your grid has none. |
| Capacity tariff minimum | `capacity_min_kw` | 2.5 kW | The lowest monthly peak that is billed. |

### Battery

| Setting | Key | Default | What it does |
| --- | --- | --- | --- |
| Battery capacity | `battery_capacity_kwh` | 16 kWh | Energy between 0 and 100 % SOC. |
| Battery minimum SOC | `battery_min_soc` | 10 % | The inverter's own floor; never planned below. |
| Battery reserve for export | `battery_reserve_soc` | 35 % | Battery energy is never exported below this; the house can still use the battery down to the minimum. |
| Battery nominal voltage | `battery_voltage` | 51.2 V | Amps ↔ kW when there is no voltage sensor (or it reads below 40 V). |
| Maximum charge current | `max_charge_current_a` | 50 A | Highest charge current Battery Pilot sets. |
| Maximum discharge current | `max_discharge_current_a` | 90 A | Highest discharge current when exporting. |
| Round-trip efficiency | `round_trip_efficiency_pct` | 90 % | Energy back out per 100 kWh in; charging to use or sell later must beat this loss. |
| Battery wear cost | `wear_cost_c_kwh` | 2.0 c/kWh | Cost of every kWh taken out of the battery: replacement cost ÷ (rated cycles × capacity). Counted in the plan and the comparison. |

### Planning

| Setting | Key | Default | What it does |
| --- | --- | --- | --- |
| Winter day when solar is below | `winter_solar_threshold_kwh` | 12 kWh | A day whose shade-corrected forecast is below this is a winter day. |
| Winter strategy | `winter_mode` | full_day | `full_day`: on winter days fill the battery at night to carry the day, no battery export. `peaks_only`: keep only enough for your usual peaks (see below). |
| Night target safety margin | `safety_margin_pct` | 30 % | SOC points added to what the day needs, for `full_day`. Raise it if the battery runs empty before night on cloudy days. |

### Inverter and alerts

| Setting | Key | Default | What it does |
| --- | --- | --- | --- |
| Solis slot entities | `slot_entities` | time_charging | Which set of time-of-use entities to drive; switch to `grid_time_of_use` if charging does nothing. |
| Phone notification action | `notify_service` | (empty) | For example `notify.mobile_app_pixel_8`. Empty: the only Companion app phone found. |

The entity settings (`grid_import_power`, `battery_soc`, …) are in *What Battery Pilot
reads* above; leave them empty to let Battery Pilot find them.

### Winter strategy

`full_day` (the default) is what Battery Playbook did: on a dull day the battery is filled
overnight to carry the whole day. On a day/night contract where the night rate is only a
few cents cheaper, that loses money: about 10 % of every kWh is lost in charging and
discharging, plus battery wear. `peaks_only` keeps just enough for your capacity peaks and
was cheaper in the year simulation (see the changelog), with nearly the same peaks. Try it
once Battery Pilot has learned your peaks (a week or two).

## Sensors it publishes

| Entity | State | Main attributes |
| --- | --- | --- |
| `sensor.battery_pilot_plan` | What the battery does now: `self_use`, `charge` or `discharge` | `mode` (shadow/active), `soc_target`, `reason`, `winter`, `plan_cost_eur`, `next_hours` (the next 24 hours) |
| `sensor.battery_pilot_night_target` | SOC needed when the night window ends (%) | `winter`, `need_kwh`, `solar_kwh` |
| `sensor.battery_pilot_solar_today` | Shade-corrected solar forecast for today (kWh) | `forecast_kwh` (before correction) |
| `sensor.battery_pilot_solar_tomorrow` | The same for tomorrow | `forecast_kwh` |
| `sensor.battery_pilot_quarter_projection` | Where this quarter-hour's average import is heading (kW) | |
| `sensor.battery_pilot_month_peak` | This month's highest quarter-hour (kW) | `billed_kw`, `month_cost_eur`, `rolling_12_month_kw` |
| `sensor.battery_pilot_buy_price` | Buy price now on your contract (c/kWh) | `contract`, `fixed`, `dynamic` (buy and sell on both), `market_eur_mwh` |
| `sensor.battery_pilot_sell_price` | Sell price now on your contract (c/kWh) | `contract` |
| `sensor.battery_pilot_contract_comparison` | Dynamic minus fixed over the stored months (€; negative = dynamic cheaper) | `basis`, `fixed_eur`, `dynamic_eur`, `same_usage`, `with_strategy` |

## Emergency stop

Switch off **Battery Pilot control**. Within seconds the inverter is back in self-use
and Battery Pilot only plans.

## Limits

- Solis hybrids through the Solis Modbus integration only.
- Public holidays are not treated as night tariff.
- The comparison needs a price sensor, and the best-battery-use view a full day of data;
  the first figures appear the morning after the first full day.
