# Changelog

## 2.4.0

**Planned vs actual is easier to read**, on a phone above all:

- **The day in words** at the top: what differed from the forecasts (the house used more or
  less than expected, the panels gave more or less) and where the battery is against the
  plan. Shadow mode is one short note instead of a large banner.
- **A small table of figures** (expected, measured, difference for solar, house and the
  battery) replaces the eight number tiles.
- **One chart** instead of two: the day-ahead plan. How far each hour's own plan was off is
  one figure under the table, and every hour is still in the hourly table.
- **New House panel**: the consumption per hour against what was expected, on the same
  kWh scale as the solar. The panels have titles, and one rule holds for all of them:
  solid line or bars = measured, dashed = expected. The legend shows only the strip colours
  the day has; the strips and their labels are larger.
- The list of recent days has four columns that fit a phone, with the house consumption.
- The explanation sits under *How to read this*.

## 2.3.3

- The charts on the page now work on a phone (the Home Assistant app has no hover). Tap
  a chart for the values at that point, or slide a finger sideways along it. The details
  stay until you tap elsewhere. This covers all of them:
  - **The 36-hour plan:** the time, what the battery does, the buy and sell price, the
    battery level, the expected solar, house and grid energy, and the night target.
  - **Planned vs actual:** as before with the mouse, now also by tapping.
  - **Solar and tree shade:** the month and hour, how much of the forecast the panels
    deliver there and how much forecast energy that was learned from, or that nothing is
    learned yet. The arrow keys move through months and hours.
  - **Capacity peaks:** the month, its highest quarter-hour and the import limit.
- The plan card explains its chart: the bar height is the buy price (left axis), and the
  circle on the battery line is the night target.

## 2.3.2

- The dashboard no longer shows "Cannot reach Battery Pilot: SyntaxError …" when it is
  opened again after the browser or phone slept. Home Assistant's 15-minute ingress session
  had lapsed and its plain-text "401: Unauthorized" was read as data. The dashboard now
  updates as soon as it is visible again, retries within seconds while Home Assistant renews
  the session, and only then warns, in words (session lapsed, app restarting, no
  connection), with the time of the figures still on the page.

## 2.3.1

- The app reports its health to Docker (a health check on its own web server that also
  notices a stalled main loop), replacing the obsolete `watchdog` option. Keep
  **Watchdog** switched on in the app's Info tab: Home Assistant then restarts Battery
  Pilot when it crashes or stops responding, and it puts the inverter back in self-use.

## 2.3.0

Safety and reliability, from a new set of tests that run whole days and failures
through the app:

- **Lost or frozen measurements stop a charge or discharge.** Without the battery SOC
  or grid import for a minute (or a SOC that does not move for 30 minutes while charging
  or discharging), the inverter goes back to self-use until the readings return. Before,
  it kept going until the end of the hour, with no peak protection. A missing
  house-load reading alone no longer stops a night charge.
- **The inverter is never left charging.** The Solis repeats its time-of-use slot every
  day: after a crash or power cut the app (now restarted by the Supervisor watchdog) puts
  it back in self-use, and a failed command no longer leaves it unattended when control
  is switched off. The time-of-use switch is read back and corrected if something else
  changed it while control is on.
- A single failed read of the control switch no longer interrupts a charge.
- **Zero-export installations stay at zero export** through a negative-price block; the
  export limit found before a block is restored, not the setting.
- **Steadier inverter commands:** the plan no longer switches between discharge and
  self-use several times an hour, and a rising house load lowers the charge current
  instead of pausing and restarting the charge.
- A boost above the import limit is announced once, and its higher limit only applies to
  the boosted hours.
- **The comparison is fair when price data is missing:** such quarter-hours are priced
  with the day before on both contracts (they made dynamic look cheaper), and marked.
- A solar forecast outage keeps the last forecast instead of planning a winter night.
- A slow Home Assistant no longer holds up the 10-second control loop.
- The monthly verdict and renewal reminders catch up after a day the app wasn't running.
- A damaged `state.json` no longer stops the app; a broken settings file gives a clear
  message. The first plan after a start uses the real SOC. Price times without a time
  zone are read in HA's time zone.

## 2.2.0

- **Price spikes earn money.** At a very low or very high price the battery now charges
  above the import limit, as fast as it takes, when that earns more than the higher
  capacity peak costs. The cost is worked out the way Fluvius bills it: the month's
  highest quarter-hour, at least the 2.5 kW minimum, counts once in the 12-month average.
  A peak this month already has costs nothing extra. Without a capacity tariff the import
  limit is never crossed.
- At a price so high that buying the energy back later costs less, the battery is sold
  below the reserve, down to its minimum, and also on a dull winter day (which otherwise
  still never exports).
- The plan card says when the plan does either, and why. A planned higher peak no longer
  triggers the peak warning; the plan sensor has a `boost_kw` attribute.
- The nightly fixed-vs-dynamic replay does the same, so the comparison counts these gains
  and the higher peaks.
- *Normal export limit* now defaults to 5 kW, the Fluvius limit for a single-phase
  connection. **An existing installation keeps its saved value: check it on the
  Configuration tab**, because it is what Battery Pilot restores on the inverter after
  blocking export at a negative price.
- Tests confirm that the battery is sold down before a sunny day that fills it anyway,
  in the best-paid hours, keeping what the house needs until the shade-corrected sun covers
  it.

## 2.1.0

- **Planned vs actual.** A new card on the page shows, day by day, what the plan
  predicted and what really happened: the battery level, the solar (forecast before and
  after the shade correction, and measured) and what the inverter did. One chart shows
  the plan made just after midnight, the other the plan in force at the start of each
  hour. The last 60 days are kept, with a summary per day.
- The plan is redone right after the nightly learning, so it uses the new shade and
  consumption factors at once.
- Dark mode: the battery line is a little darker, for contrast.

## 2.0.2

- Fix: the app could not reach Home Assistant and stayed on "waiting for Home
  Assistant", because the Supervisor token was not passed to it.
- When Home Assistant does not answer, the panel and the log now say why.

## 2.0.1

- The fixed-vs-dynamic comparison follows *Battery Pilot block negative export*: with
  it on, the nightly replay blocks export at negative sell prices, as the app does, so
  a dynamic contract is no longer shown as paying for those exports.
- The plan only counts on blocking negative export when an export-limit entity was
  found, since without one nothing gets blocked.

## 2.0.0

Battery Playbook is now **Battery Pilot**, an always-running app with its own page in
the sidebar. Install Battery Pilot; it takes over from Battery Playbook by itself.

- Plans the battery for 36 hours with an optimizer over prices, losses, wear and the
  capacity tariff, instead of fixed rules. Works for fixed and dynamic contracts.
- Learns tree shade per month and hour of the day instead of one rolling factor.
- Compares fixed and dynamic contracts on your measured usage, and by replaying each
  day with the best battery strategy for each contract (no more estimated monthly gains).
- Every contract, grid, battery and tariff value is a setting; the old Flemish values
  are the defaults.
- Learns your usual quarter-hour peaks per weekday and hour and keeps enough battery to
  cover them under the import limit.
- New winter strategy setting: `full_day` (as before: fill at night to carry the day) or
  `peaks_only` (keep just enough for the peaks). In a simulated year with the default
  settings, `peaks_only` was about €38 cheaper on the fixed contract with the same peaks.
- Starts in shadow mode; switch on "Battery Pilot control" when you trust the plan.
- Returns the inverter to self-use when the app stops; never changes the export limit
  unless you switch on "Battery Pilot block negative export".
- Fixed: grid-charged to 100 % when the battery was already above the night target.
- Fixed: the night target was only checked when the window started, so overnight drain
  was never topped up. It is also capped at what the night window can charge under the
  import limit, so an unreachable target no longer buys at the day rate in the evening.
- Fixed: a missing PV sensor taught the shade correction that it was always dark.
- Fixed: helper changes made in the HA UI were reset on every restart.
- Fixed: the monthly peak was not reset if HA was down at midnight on the 1st.
- Peak warnings now also go to your phone.

## 1.0.0 (Battery Playbook)

- First version: peak shaving, dynamic-tariff rules in shadow mode, contract watch.
