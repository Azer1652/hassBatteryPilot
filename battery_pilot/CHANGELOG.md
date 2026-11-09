# Changelog

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
- Starts in shadow mode; switch on "Battery Pilot control" when you trust the plan.
- Fixed: grid-charged to 100 % when the battery was already above the night target.
- Fixed: the night target was only checked when the window started, so overnight drain
  was never topped up.
- Fixed: a missing PV sensor taught the shade correction that it was always dark.
- Fixed: helper changes made in the HA UI were reset on every restart.
- Fixed: the monthly peak was not reset if HA was down at midnight on the 1st.
- Peak warnings now also go to your phone.

## 1.0.0 (Battery Playbook)

- First version: peak shaving, dynamic-tariff rules in shadow mode, contract watch.
