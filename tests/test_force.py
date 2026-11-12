"""Charging and export through the Solis Modbus force action (`charge_command`)."""
from datetime import datetime, timedelta, timezone

import pytest
from fake_ha import CHEAP_NIGHT, TZ, FakeHA, utc

from pilot import discovery
from pilot.app import App
from pilot.config import ConfigError, Options, from_dict
from pilot.controller import Readings, decide
from pilot.planner import Step
from pilot.solis import FORCE_MINUTES, FORCE_RENEW_S, Solis

CONTROL = "input_boolean.battery_pilot_control"
FILL = "input_boolean.battery_pilot_fill_battery_for_car"
FORCE = {"domain": "solis_modbus", "services": {s: {} for s in discovery.FORCE_SERVICES}}
T = datetime(2026, 1, 15, 1, 10, tzinfo=TZ)


# ---------------------------------------------------------------- choosing the method
def test_the_method_follows_the_setting_and_what_home_assistant_has():
    assert discovery.charge_method(Options(), [FORCE])[0] == "force"
    assert discovery.charge_method(Options(), [])[0] == "time_slot"
    assert discovery.charge_method(Options(charge_command="time_slot"), [FORCE])[0] == "time_slot"
    method, why = discovery.charge_method(Options(charge_command="force"), [])
    assert method == "time_slot" and "no solis_modbus.solis_force_battery_charge" in why
    partial = {"domain": "solis_modbus", "services": {"solis_force_battery_charge": {}}}
    assert discovery.charge_method(Options(), [partial])[0] == "time_slot"     # an older integration
    with pytest.raises(ConfigError):
        from_dict({"charge_command": "modbus"})
    assert from_dict({"solis_slave": 2}).solis_slave == 2


def test_with_the_force_action_the_time_of_use_entities_are_optional():
    fake = FakeHA(utc(2026, 1, 14, 12, 0))
    states = {k: v for k, v in fake.s.items() if "time_charging" not in k and "time_of_use" not in k
              and "allow_grid" not in k}
    _, missing = discovery.resolve_all(Options(), states)
    assert "charge_start" in missing and "tou_switch" in missing
    _, missing = discovery.resolve_all(Options(), states, force=True)
    assert missing == []


# ---------------------------------------------------------------- the commands
class HA:
    def __init__(self):
        self.calls = []

    async def call(self, domain, service, data):
        self.calls.append((domain, service, data))


async def test_force_commands_and_their_renewal():
    ha = HA()
    s = Solis(ha, {"tou_switch": "switch.tou", "peak_shaving_switch": "switch.shave"}, method="force", volts=51.2)
    await s.charge(T, 30)
    assert ("switch", "turn_off", {"entity_id": "switch.tou"}) in ha.calls
    assert ha.calls[-1] == ("solis_modbus", "solis_force_battery_charge",
                            {"power_watts": 1500, "duration_minutes": FORCE_MINUTES})
    assert s.mode == "charge" and s.amps == 30 and not s.renew_due(T + timedelta(minutes=5))
    assert s.renew_due(T + timedelta(seconds=FORCE_RENEW_S))
    await s.set_current(T, 50)
    assert ha.calls[-1][1] == "solis_force_battery_charge" and ha.calls[-1][2]["power_watts"] == 2600
    ha.calls.clear()
    await s.self_use(T)
    assert ha.calls[0] == ("solis_modbus", "solis_stop_force_charge_discharge", {})
    ha.calls.clear()
    await s.peak_shave(T, 2.3, 10)                        # already stopped: not sent again
    assert not [c for c in ha.calls if c[0] == "solis_modbus"]
    s.slave = 3
    await s.discharge(T, 40)
    assert ha.calls[-1] == ("solis_modbus", "solis_force_battery_discharge",
                            {"power_watts": 2000, "duration_minutes": FORCE_MINUTES, "slave": 3})
    assert not s.renew_due(T) and Solis(ha, {}).renew_due(T) is False       # (time slots: never)


def test_a_running_force_command_is_sent_again_before_it_lapses():
    o = Options()
    t = T.astimezone(timezone.utc)
    r = Readings(t, T, 40.0, 0.5, 0.0, 1.5, 1.5, 51.2)
    step = Step(t.replace(minute=0), 1.0, "charge", 40.0, 100.0, 0, 0, 0, 0, 0, 0, 0)
    hour = T.strftime("%H:00:00")
    assert decide(step, r, o, "charge", hour, 30, True).command == "none"
    d = decide(step, r, o, "charge", hour, 30, True, renew=True)
    assert d.command == "charge" and d.reason == "renew the force command"


# ---------------------------------------------------------------- the whole app
async def force_app(tmp_path, **kw):
    fake = FakeHA(utc(2026, 1, 14, 14, 0), soc=40, force_action=True, **kw)
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(), fake, data_dir=str(tmp_path / "d"), clock=lambda: fake.now)
    assert await app.setup()
    assert app.solis.method == "force"
    return app, fake


async def run(app, fake, end, step_s=30):
    while fake.now < end:
        await app.step()
        fake.advance(timedelta(seconds=step_s))


@pytest.mark.slow
async def test_the_fill_switch_charges_through_the_force_action(tmp_path):
    app, fake = await force_app(tmp_path)
    await app.set_fill(True)
    await run(app, fake, utc(2026, 1, 14, 15, 0))
    assert fake.mode() == "force_charge" and app.status()["car"]["fill_state"] == "filling"
    assert fake.soc > 45
    sent = [c for c in fake.calls if c[1] == "solis_force_battery_charge"]
    assert 4 <= len(sent) <= 12                          # renewed about every 10 minutes, not every tick
    assert not [c for c in fake.calls if c[0] == "time"]         # the slots are not touched
    assert fake.s["switch.solis_time_of_use"]["state"] == "off"
    limit = Options().import_limit_kw
    assert max(r.kwh["import"] * 4 for r in app.records) <= limit + 0.15      # under the import limit
    await app.set_fill(False)
    await run(app, fake, utc(2026, 1, 14, 15, 2))
    assert fake.forced is None and fake.mode() == "self_use"


@pytest.mark.slow
async def test_a_winter_night_charge_goes_through_the_force_action_and_lapses_after_a_crash(tmp_path):
    fake = FakeHA(utc(2026, 1, 14, 23, 0), soc=20, force_action=True,
                  forecast={datetime(2026, 1, 15, h, tzinfo=TZ).astimezone(timezone.utc): 0.3 for h in range(9, 16)})
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(**CHEAP_NIGHT), fake, data_dir=str(tmp_path / "d"), clock=lambda: fake.now)
    assert await app.setup()
    while fake.mode() != "force_charge" and fake.now < utc(2026, 1, 15, 7, 0):
        await app.step()
        fake.advance(timedelta(seconds=60))
    assert fake.mode() == "force_charge" and app.solis.mode == "charge"
    soc = fake.soc
    for _ in range(FORCE_MINUTES + 1):                   # the app is gone: nothing renews the command
        fake.advance(timedelta(seconds=60))
    assert fake.mode() == "self_use" and fake.soc > soc


async def test_stopping_the_app_stops_the_force_command(tmp_path):
    app, fake = await force_app(tmp_path)
    await app.set_fill(True)
    await run(app, fake, utc(2026, 1, 14, 14, 3))
    assert fake.mode() == "force_charge"
    await app.shutdown()
    assert fake.forced is None and ("solis_modbus", "solis_stop_force_charge_discharge", {}) in fake.calls[-3:]
