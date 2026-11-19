# Not verified on real hardware

Everything is tested against the simulated house, battery and inverter in `tests/fake_ha.py`.
The points below rest on that model or on documentation, and have not been confirmed on a real
installation. Do not present them to the owner as established; when one is confirmed or refuted,
update this file.

| Area | What is assumed | Where |
| --- | --- | --- |
| Solis peak shaving | With the peak-shaving switch on, the grid feeds the house up to the set power and the battery gives only what is above it; it does not discharge below that power; solar surplus still charges the battery. | `solis.Solis.peak_shave`, `controller.decide` |
| Solis feed-in priority | With the switch on, solar surplus is exported and not stored, and the battery does not feed the house. | `solar_hold` in `planner.py`, `controller.py` |
| Peak buffer | The battery stops at minimum + buffer, shaves below it and is charged back under the import limit. | `controller.decide`, `simulate.step` |
| House load and the car | The house-load sensor includes the car charger, so a load step means the car. | `car.LoadStepSource` |
| Car already charging at start | The learned usual load for the hour is close to the real house load, so a car charging when the app starts is seen against it (tested in `fake_ha` only). | `car.LoadStepSource` (`usual_kw`) |
| Force peak shaving switch | Peak shaving held for hours by hand behaves like the short peak-shaving periods the plan uses (tested in `fake_ha` only). | `controller.decide` (`force_shave`) |
| Solis force action | `solis_force_battery_charge` / `_discharge` charges or discharges at `power_watts` measured on the battery side (amps × battery voltage), from the grid without *allow grid charging*, until its timeout or the stop action, and nothing else on the inverter interferes. Battery Pilot's own current control keeps import under the limit; that the inverter itself respects peak-shaving limits during a force charge is not assumed. The owner confirmed by hand that the action charges the battery. | `solis.Solis` (`method == "force"`) |
| Peak-shaving baseline SOC | With peak shaving on, a Solis stops discharging at `peak_baseline_soc` and refills from the grid below it. Battery Pilot raises it to minimum + peak buffer while the car charges, so the buffer is kept for the house. | `App.shave_level`, `tests/fake_ha.py` |
| Fill switch | Charging to 100 % in the current clock hour and holding with peak shaving behaves as simulated. | `controller.fill_step` |
| Saving settings | `POST /addons/self/options` on the Supervisor is allowed with `hassio_api: true` and accepts the merged options. | `App.save_options`, `HA.supervisor` |
| A slot across midnight | The 23:00 charge or discharge slot is set as 23:00–00:00 (it was 23:00–23:59, which left the last minute in self-use). A Solis takes a night window such as 22:00–06:00, so a slot whose end is before its start is assumed to run across midnight; `tests/fake_ha.py` models it that way. | `solis.hour_slot` |
| Slots repeat daily | The Solis repeats a charge or discharge slot every day, so the app sets the current clock hour only and returns the inverter to self-use at a restart. | `solis.py`, `App` (`inverter.json`) |
| Home Assistant restart | The inverter keeps its setting while Home Assistant restarts, and the Solis Modbus integration does not write registers when it loads. The owner's log (peak shaving before and after a 30-second restart) fits this; it is not confirmed. Battery Pilot sends its command again when HA is back either way. | `App._ha_away`, `App._ha_back` |
| Blocking export | Setting the export-limit entity to 0 stops export at negative prices and the inverter throttles the panels. | `App._curtail` |
| The page in the Companion app | Tabs, the file download and the file picker work inside the app's web view. | `static/index.html` |
| Inverter's own use | `import − household load − export` with the battery idle and no sun is the inverter's own draw (the Solis household load excludes it), and the draw is the same while the battery charges or discharges. *Battery Power Net* is DC, + discharging, as in the Solis Modbus integration's source; the owner's export of it (S6-EH1P) fits that sign. | `efficiency.EfficiencyMeter` (`_learn_draw`, `report`) |
| Solis Battery Saving | Off. Solis's quick-setting guide for forced charge/discharge times says to turn it off with time-of-use; with it on, the inverter takes its standby power from the grid at night. How it interacts with Battery Pilot's time-of-use slots and force action was never tried. | `battery_pilot/DOCS.md` (*Measured efficiency*) |
| Marstek passive mode | `set_passive_mode` with a negative power charges from the grid at that AC power, a positive one discharges (also exporting), and when its duration ends the battery returns to the mode it had (Auto). Pressing *Auto mode* ends a passive command. From the integration's source and README only; never run against a real Venus. | `marstek.Marstek` |
| Marstek operating-mode select | A select `…_operating_mode` with an *Auto* option switches the battery to Auto mode, like the button (seen on an owner's install, not in the integration's source we read). | `marstek.Marstek.self_use`, `auto_option` |
| Marstek readings | The Local API's *Power* is + charging (the integration's code; its README agrees), and its *Last message received* grows while the link is down (the coordinator keeps old values). The cloud's *Report Time* is an epoch time or an ISO date; anything else is ignored. | `marstek.choose`, `marstek.age_s`, `App._battery` |
| Marstek peak shaving by Battery Pilot | Passive power set from the grid meter (only changes of 200 W, at most once a minute, every 30 s when 0.5 kW over the limit) keeps the grid near the import limit; the device accepts a passive command renewed every 120 s and a follow command at most every 60 s without becoming unresponsive. Built on the power last sent, not on the battery's reported power. | `marstek.Marstek.follow`, `shave_power` |
| HomeWizard P1 | *Power* is + import; *Average demand* is the running quarter-hour's average so far (the Belgian meter's 1.4.0); *Peak demand current month* is the month's highest quarter-hour. | `meter.QuarterMeter.projection_kw`, `PeakTracker.observe` |
| Solax house load with a Marstek | The Solax Modbus *House Load* (inverter power − grid feed-in) counts an AC-coupled Marstek's charging as consumption, so adding the battery's power back gives the real house load. | `App.tick` (`marstek_load_includes_battery`) |
| ENTSO-e price list | The list is an attribute of the *Average electricity price* sensor (read from the integration's source, version 0.7.5). | `prices.py`, `App.refresh` |

A price modifier template in the ENTSO-e integration (VAT or fees added) makes the dynamic
contract look dearer than it is: the app expects the bare market price and applies the contract
formula itself. The app cannot detect this.
