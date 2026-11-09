# Changelog

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
