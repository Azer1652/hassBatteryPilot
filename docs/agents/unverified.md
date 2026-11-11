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
| Fill switch | Charging to 100 % in the current clock hour and holding with peak shaving behaves as simulated. | `controller.fill_step` |
| Saving settings | `POST /addons/self/options` on the Supervisor is allowed with `hassio_api: true` and accepts the merged options. | `App.save_options`, `HA.supervisor` |
| Slots repeat daily | The Solis repeats a charge or discharge slot every day, so the app sets the current clock hour only and returns the inverter to self-use at a restart. | `solis.py`, `App` (`inverter.json`) |
| Blocking export | Setting the export-limit entity to 0 stops export at negative prices and the inverter throttles the panels. | `App._curtail` |
| The page in the Companion app | Tabs, the file download and the file picker work inside the app's web view. | `static/index.html` |
| ENTSO-e price list | The list is an attribute of the *Average electricity price* sensor (read from the integration's source, version 0.7.5). | `prices.py`, `App.refresh` |

A price modifier template in the ENTSO-e integration (VAT or fees added) makes the dynamic
contract look dearer than it is: the app expects the bare market price and applies the contract
formula itself. The app cannot detect this.
