# Battery Pilot

Battery Pilot steers a home battery on a Solis hybrid inverter so it costs you as
little as possible, and tells you whether a fixed or a dynamic electricity contract
would be cheaper for your house. It runs all the time and has its own page in the
sidebar.

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
| Dull day ahead (winter), strategy `full_day` | Charged from the grid at night, at the cheapest moments, to the **night target** by the time the night tariff ends. The target is what the house needs until evening after the corrected solar, plus the safety margin. No battery export. |
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

## Settings

Every setting is explained on the Configuration tab. The defaults are an Eneco
contract on a Fluvius connection in Flanders (2026) with a 16 kWh battery; change them
to your own. The contract formulas:

- fixed buy = day or night rate + grid fees
- fixed sell = *fixed: sell price × market* × the month's average market price +
  *fixed: sell price +* − injection fee
- dynamic buy = (*buy × market* × market price + *buy +*) × (1 + VAT) + grid fees
- dynamic sell = *sell × market* × market price + *sell +* − injection fee

Market prices are in EUR/MWh. For a flat sell rate, set the × market setting to 0 and
put the rate in the + setting.

### Winter strategy

`full_day` (the default) is what Battery Playbook did: on a dull day the battery is filled
overnight to carry the whole day. On a day/night contract where the night rate is only a
few cents cheaper, that loses money: about 10 % of every kWh is lost in charging and
discharging, plus battery wear. `peaks_only` keeps just enough for your capacity peaks and
was cheaper in the year simulation (see the changelog), with the same peak. Try it once
Battery Pilot has learned your peaks (a week or two).

## Sensors

| Entity | What it shows |
| --- | --- |
| `sensor.battery_pilot_plan` | Now: self_use, charge or discharge; attributes: target SOC, reason, the next 24 hours |
| `sensor.battery_pilot_night_target` | SOC needed at the end of the night window |
| `sensor.battery_pilot_solar_today` / `_tomorrow` | Shade-corrected solar forecast |
| `sensor.battery_pilot_quarter_projection` | Where this quarter-hour's average import is heading |
| `sensor.battery_pilot_month_peak` | This month's capacity peak, billed kW and cost |
| `sensor.battery_pilot_buy_price` / `_sell_price` | Current prices on your contract (both contracts in the attributes) |
| `sensor.battery_pilot_contract_comparison` | Dynamic minus fixed over the stored months |

## Emergency stop

Switch off **Battery Pilot control**. Within seconds the inverter is back in self-use
and Battery Pilot only plans.

## Limits

- Solis hybrids through the Solis Modbus integration only.
- Public holidays are not treated as night tariff.
- The comparison needs a price sensor, and the best-battery-use view a full day of data;
  the first figures appear the morning after the first full day.
