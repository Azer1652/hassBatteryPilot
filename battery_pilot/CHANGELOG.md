# Changelog

## 2.11.1

- **Default rates brought up to Eneco's November 2026 card** (Zon & Wind Dynamisch, Fluvius
  Antwerpen): dynamic buy price + 0.408 c/kWh (was 1), dynamic sell price − 1.3 c/kWh (was − 1.188),
  dynamic contract fee € 15 per year (was € 100), grid fees and taxes 11.33 c/kWh (was 11.96).
  Settings you already saved are not changed by an update: change them on the Configuration tab or
  with an import.

## 2.11.0

- **Export and import the settings.** A new *Settings* tab on the page: *Export settings*
  keeps the settings of the Configuration tab as a file (or as text to copy). An import first
  lists what would change; *Save these settings* saves them, and they are used after you restart
  the app. Settings missing from the file keep their value. What the app learned is not in the file.
- **Tabs.** The page shows one part at a time: *Plan*, *Planned vs actual*, *Fixed or dynamic*,
  *Solar*, *Peaks*, *Log* and *Settings*. The figures at the top stay in view on every tab, and the
  page opens on the tab you used last.
- The app now asks for access to the Supervisor API (`hassio_api`), to save imported settings.

## 2.10.1

- **ENTSO-e prices were not read.** The ENTSO-e integration keeps its price list on the
  *Average electricity price* sensor; Battery Pilot picked *Current electricity market price*,
  which has none. The result: no dynamic prices and a comparison that stayed on "collecting
  data", without a word in the log. The right sensor is now found, a setting that still names
  the other one is followed to it, and the log warns when a price sensor has no price list.

## 2.10.0

**A buffer in the battery for short peaks.**

- **New setting** *Peak buffer* (`peak_buffer_pct`, off by default): SOC points above the
  battery minimum that the house may not use up. 10 is a good value.
- **How it works.** Self-use stops at minimum + buffer. At or below that level the inverter
  goes to peak shaving: the grid feeds the house up to the import limit and the battery
  gives only what is above it. Before, a battery at its minimum left a kettle-and-hob peak
  to the grid, which sets the capacity tariff.
- **Refill.** When a peak has taken 3 points or more out of the buffer, it is charged back
  from the grid with import kept at the import limit. Solar surplus fills it as well.
- **Needs the inverter's peak-shaving switch.** Without it the setting is ignored and the
  log says so.
- The plan, the nightly replay and the comparison count with the buffer: the plan chart
  shows peak shaving where the battery sits on it. The refill from the grid is not in the
  plan or the replay.
- The buffer is taken out of daily use: with 10 %, that share of the battery is no longer
  cycled.

## 2.9.0

**A button to fill the battery before the car charges.**

- **The button** *Fill the battery for the car* is on the Battery Pilot page, under the plan.
  It is a normal helper, `input_boolean.battery_pilot_fill_battery_for_car`, created at the
  first start after the update, so it also works from a dashboard or an automation.
- **While it is on** (and *Battery Pilot control* is on) the battery is charged from the grid
  to 100 %, with grid import kept at the import limit, and then kept full: with the
  inverter's peak-shaving switch the grid feeds the house and the battery only what is
  above the limit; without it the charge slot stays on at the minimum current.
- **When the car charges**, the 2.8.0 rule applies: the grid gives up to the import limit
  and the full battery the rest.
- **It turns itself off** when the car has charged (with car detection on) or after
  24 hours. Turn it off yourself at any time; the plan is followed again.
- It works without car detection (`car_min_kw`); the switch then ends after 24 hours or by hand.
- The plan on the page does not show the fill; the line next to the button says what it
  is doing.

## 2.8.0

**Battery Pilot knows when the car charges and shaves its peak.**

- **Off by default.** Set *Electric car: smallest charging power* (`car_min_kw`) to switch
  it on. For a charger limited to 5 kW, about 3.5 is a good value. Or set the optional
  entity `car_charger_power` (a power sensor of the charger, never found automatically);
  then `car_min_kw` is not needed.
- **How it sees the car.** Without the charger sensor, a jump in house consumption of at
  least `car_min_kw` above the level of the last 10 minutes, lasting 3 minutes, is the car.
  The session ends when consumption is below that level plus half of `car_min_kw` for
  2 minutes. With the charger sensor, charging is anything above 1 kW.
- **What it does.** While the car charges and the house asks more than the import limit,
  and the inverter's peak-shaving switch was found, the inverter goes to peak shaving
  whatever the plan says: the grid gives up to the import limit and the battery the rest.
  In self-use the battery would feed the whole car, run empty and leave the full charger
  power to the grid, a capacity peak. When the car stops, the plan is followed again.
  Without the switch the car is only measured.
- The car's energy is kept out of the house consumption and the usual peaks that Battery
  Pilot learns. While a session runs, the plan counts the car on top of the house for the
  next hour. The nightly replay uses peak shaving in quarter-hours in which the car charged.
- New sensor `sensor.battery_pilot_car_power` (only with car detection on), a line in the
  plan card, `car` in `/api/status`, and events when a session starts and ends.
- **Limits.** Battery Pilot does not control the charger. A long burst of a hob or oven can
  be taken for a car: the peak is then shaved while cooking, which is harmless, and the
  burst is not learned as house consumption. Slow sessions below `car_min_kw`, for example a charger in
  solar-surplus mode, are not detected without the charger sensor. Not yet checked on a
  real installation: that the inverter's house-load sensor includes the charger. The car's
  own battery level is not read; the code is ready for a car integration.

## 2.7.0

**The plan follows today's weather.**

- **Today's solar forecast is corrected as the day goes.** Until now the forecast was only
  corrected for shade, an average over weeks. Now the plan also compares what the panels
  gave in the last three hours with the forecast after shade, and plans the rest of today
  lower or higher by that ratio (between 0.3 and 1.6). The correction fades with a
  half-life of four hours and never touches tomorrow, so the winter rule and the night
  target are unchanged. Quarter-hours with missing data, with export blocked or at the
  export limit do not count: the inverter held the panels back then. Shade learning is
  unchanged. The nightly replay plans the same way.
- The page shows a line when today differs from the forecast by 15 % or more, and
  `sensor.battery_pilot_solar_today` has the attribute `today_factor`.
- The gain is small. In the year simulation with a forecast that is wrong about the day
  (`tools/yearsim.py --forecast-error`), the dynamic contract goes from €874 to €869 and
  the fixed contract stays at €947. With a forecast that is right about the day it changes
  nothing on fixed and costs €1 on dynamic. Other window lengths and half-lives gave the
  same result within €1.
- **Why it trades, or not.** The plan card now shows the best trade of the next 24 hours
  with your own rates: when to buy and sell, what one bought kWh earns or loses after the
  conversion loss and battery wear, and the selling price from which it would pay. With
  the default rates, a kWh bought at a market price of 0 must be sold at about 180 €/MWh
  to break even, because the grid fees sit on every kWh bought. It also shows what
  charging in the cheapest hour to avoid buying in the dearest one saves. The plan itself
  is unchanged: it already made these trades when they pay.
- Not new: the plan already keeps battery for your usual consumption peaks (the typical
  highest quarter-hour per weekday and hour), since 2.0.

## 2.6.0

**Solar hold, and the battery's measured efficiency.**

- **Solar hold.** When the Solis Modbus integration has the feed-in priority switch
  (`…feed_in_priority`), the plan can use it: while there is solar surplus the surplus is
  exported and not stored, and the battery keeps its room for later hours in which surplus
  cannot be sold (a negative sell price with export blocked, or more surplus than the
  export limit). The plan picks it only where it lowers the cost. Battery Pilot switches it
  on with at least 0.3 kW of surplus and returns to self-use when the surplus is gone,
  because the battery may not feed the house in this mode. Stopping the app, switching
  control off, lost measurements and a restart all end it. The dashboard shows it in its
  own colour. It is not yet checked on a real inverter: on the first sunny day, check that
  the switch goes on in the morning, that export shows, and that the battery charges again
  at midday. Without the switch nothing changes.
- **Measured efficiency.** Every night the app measures the battery's real round-trip
  efficiency from its own quarter-hour records (battery flow against the SOC change, SOC
  15-95 %, half-life 60 days, a value after 150 SOC points of charging and of discharging).
  The dashboard shows it next to the setting, with a sensor `sensor.battery_pilot_efficiency`.
  The plan keeps using the *Round-trip efficiency* setting: change it yourself when the
  measured value stays 3 points or more away.
- New optional entity setting: `feed_in_priority_switch` (found automatically).
- Year simulation (synthetic year, peak shaving on): the fixed contract stays at €948; dynamic goes
  from €861-862 to €856 with solar hold. It only gains on days that start with a low battery and
  have a long midday with a negative sell price.

## 2.5.0

**No night charging that loses money, and the Solis peak-shaving mode.**

- **A winter night is only filled when it pays.** On a dull day the battery was charged to
  the night target whatever it cost. On a day/night contract with a few cents between the
  rates that loses money: with the default rates a night kWh costs 28.7 c by the time it
  comes out of the battery (two conversions, plus wear) against 27.0 c saved by day. The
  target is now set only when the night price, after losses and wear, is below the day
  price. Otherwise the battery is kept for your peaks, and the plan card says why. A
  contract with a cheap enough night, or a dynamic contract, keeps its target.
- **No more small charge at the day rate in the evening.** With the refill compulsory,
  every kWh the battery gave in the evening had to be bought back at night at a loss, so
  the plan "held" the battery with a minimum grid charge at 17-18h, at the day rate. That
  came from the forced target and is gone with it.
- **Peak shaving.** When the Solis Modbus integration has the peak-shaving switch
  (`…peak_shaving_mode`), the plan can use the inverter's own peak-shaving mode: the grid
  feeds the house up to the *Grid import limit* and the battery only gives what is above
  it. The plan uses it in hours where the battery's energy is worth more later: the night
  hours when the battery cannot carry both the night and the day, and to keep the energy
  for a cooking peak instead of letting the house empty the battery and topping it up
  again. It sets the peak-shaving grid power to the import limit and the baseline SOC to
  the battery minimum, so the inverter never refills from the grid by itself. Stopping the
  app, switching control off, lost measurements and a restart all end it, as for a charge.
  The dashboard shows it in its own colour. In the simulated year it lowers a winter
  week's bill a little on both contracts and changes nothing in summer. Without the
  switch nothing changes.
- New optional entity settings: `peak_shaving_switch`, `peak_shaving_limit`,
  `peak_shaving_soc` (found automatically).
- **The simulated year** (default rates, same house and weather): fixed €988 → €955, and
  €948 with the peak-shaving switch; dynamic €885 → €875, and €862 with it. On the fixed
  contract the battery buys about 150 kWh a year from the grid instead of 1600. Without
  the switch one month's peak rose from 2.24 to 2.60 kW (a cooking peak the battery was
  too empty for); with it every month stays at the 2.3 kW limit.

## 2.4.1

**A check of the planning and comparison logic.** Seven errors fixed. Most of them only
showed on a dynamic contract or with quarter-hour prices.

- **The plan follows the battery in self-use.** The battery's change was rounded to whole
  percents in every slot, so the error always went the same way. On quarter-hour slots a
  house using 0.30 kW never moved the planned battery (after a night the plan was 24
  points too high and had "bought" 3.6 kWh that never crossed the meter), and one using
  0.35 kW emptied it twice as fast. On hourly slots it was up to 4 points. The rounding
  error is now carried to the next slot and self-use is costed with its real grid flows.
  This also makes the dynamic side of the nightly comparison plan better when your price
  sensor gives quarter-hour prices.
- **A charge is no longer stopped before every price boundary.** The plan is redone every
  5 minutes, and the minutes left before a boundary were too short to plan a charge in,
  so the inverter went to self-use and was started again a few minutes later, every
  quarter-hour (or hour). A running charge or export now carries on into the next step
  when the plan continues it. Nothing starts early: with the inverter in self-use those
  minutes stay self-use.
- **A night window inside one day** (for example 01:00–06:00) made every day a winter
  day: the day after the window was taken as empty, without solar.
- **The daily market average** was taken again from what was left of a day while its
  prices were being dropped after 3 days. With quarter-hour prices that is the dear
  evening, so the fixed contract's sell price (it follows the month's average), the fixed
  side of the replay and the break-even alerts were too high. A day's average now comes
  from the whole day only. Averages stored by an older version from quarter-hour prices
  are dropped once and rebuilt from the prices of the last days.
- **Blocked export is not tree shade.** While *Battery Pilot block negative export* holds
  export at 0, the inverter holds the panels back. Those hours were learned as shade, and
  the replay took that low output as the day's solar for both contracts. Such
  quarter-hours are now marked: no shade is learned from them, and the replay uses the
  shade-corrected forecast when it is higher than what was measured.
- **The comparison counts the same hours for both contracts.** A day replayed for the
  fixed contract only (no prices that day) is no longer added to the month. In *same
  usage*, quarter-hours without any market price (a price sensor added during the month)
  counted for fixed and not for dynamic; a month with prices is now priced on the
  quarter-hours that have one, for both.
- **Charging above the import limit:** a trial level at which nothing charged above the
  limit could win the comparison, and the real one was dropped.
- With *Dynamic: buy price × market* at 0 the dashboard and the daily alerts failed
  (division by zero in the break-even price). There is no break-even then, and it is left
  out.

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
