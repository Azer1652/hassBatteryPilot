"""A switch on the inverter changed by something else than Battery Pilot (`App._reconcile`): the event says
which switch, when, who changed it and what Battery Pilot does about it; and the Self-Use switch that
the Solis Modbus integration clears whenever peak shaving or feed-in priority goes on."""
from datetime import timedelta

import pytest
from fake_ha import TZ, FakeHA, utc

from pilot import discovery
from pilot.app import App, changed_at, changed_by
from pilot.config import Options
from pilot.solis import Solis

CONTROL = "input_boolean.battery_pilot_control"
SHAVE = "switch.solis_peak_shaving_mode"
SELF_USE = "switch.solis_self_use_mode"
T0 = utc(2026, 1, 14, 17, 0)          # a dull winter evening: the winter level (50 %) applies


async def winter_app(tmp_path, soc=51, **fake_kw):
    fake = FakeHA(T0, soc=soc, load=lambda t: 0.8, peak_shaving=True, **fake_kw)
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(battery_min_soc=10, winter_hold_soc=50), fake, data_dir=str(tmp_path / "d"),
              clock=lambda: fake.now)
    assert await app.setup()
    await run(app, fake, T0 + timedelta(hours=1))
    assert app.solis.mode == "peak_shave" and fake.s[SHAVE]["state"] == "on"    # at the level: peak shaving
    return app, fake


async def run(app, fake, until):
    while fake.now < until:
        await app.step()
        fake.advance(timedelta(seconds=10))


def drift_events(app):
    return [e["text"] for e in app.events if "not what Battery Pilot set" in e["text"]]


# ---------------------------------------------------------------- who changed it
def test_who_changed_a_state_is_read_from_its_context():
    assert changed_by({"state": "off"}) == ""                                  # no context: not said
    assert "automation or a script" in changed_by({"context": {"id": "a", "parent_id": "p", "user_id": None}})
    assert "a user or an app" in changed_by({"context": {"id": "a", "parent_id": None, "user_id": "u"}})
    assert "read from the inverter" in changed_by({"context": {"id": "a", "parent_id": None, "user_id": None}})
    assert changed_at({"last_changed": "2026-11-23T11:56:58.123+00:00"}, TZ) == "12:56:58"
    assert changed_at({"last_changed": "garbage"}, TZ) == "" and changed_at(None, TZ) == ""


async def test_switched_off_on_the_inverter_inside_the_winter_band_it_stays_in_self_use(tmp_path):
    """23/11: peak shaving at 82 % with a winter level of 80 %, and the switch read off from the inverter.
    Between the level and 5 points above it both modes are allowed, so Battery Pilot keeps self-use and says
    so (it used to say "correcting it" and send nothing)."""
    app, fake = await winter_app(tmp_path)
    fake.soc = 52.0                                     # in the band: peak shaving is kept
    await run(app, fake, fake.now + timedelta(minutes=2))
    assert app.solis.mode == "peak_shave"
    n = len(app.solis.log)
    fake.flip(SHAVE, "off")                             # no user, no automation: the integration read it
    at = fake.now.astimezone(TZ).strftime("%H:%M:%S")
    await run(app, fake, fake.now + timedelta(minutes=5))
    [text] = drift_events(app)
    assert f"peak-shaving switch went off at {at}" in text
    assert "read from the inverter" in text
    assert "Battery Pilot leaves it in self-use: self-use down to the winter level (50 %)" in text
    assert "correcting" not in text
    assert fake.s[SHAVE]["state"] == "off" and app.solis.mode == "self_use" and len(app.solis.log) == n


@pytest.mark.parametrize("who, ctx", [("a user or an app", {"user_id": "someone"}),
                                      ("an automation or a script", {"parent_id": "ctx-automation"})])
async def test_switched_off_in_home_assistant_below_the_level_it_is_switched_on_again(tmp_path, who, ctx):
    app, fake = await winter_app(tmp_path)
    fake.flip(SHAVE, "off", **ctx)
    await run(app, fake, fake.now + timedelta(minutes=2))
    [text] = drift_events(app)
    assert f"switched in Home Assistant by {who}" in text
    assert "Battery Pilot sets peak shaving: winter level: below 50 %" in text
    assert fake.s[SHAVE]["state"] == "on" and app.solis.mode == "peak_shave"


# ---------------------------------------------------------------- the Self-Use switch
class HA:
    def __init__(self):
        self.calls = []

    async def call(self, domain, service, data):
        self.calls.append((service, data.get("entity_id")))


@pytest.mark.parametrize("method", ["time_slot", "force"])
async def test_every_command_that_ends_peak_shaving_switches_self_use_on_after_it(method):
    ents = {k: f"switch.{k}" for k in ("tou_switch", "grid_charge_switch", "peak_shaving_switch",
                                       "feed_in_priority_switch", "self_use_switch")}
    ents.update({k: f"x.{k}" for k in ("charge_current", "discharge_current", "charge_start", "charge_end",
                                       "discharge_start", "discharge_end")})
    for command in ("self_use", "charge", "discharge"):
        ha = HA()
        s = Solis(ha, ents, method=method)
        await s.peak_shave(T0, 2.5, 10)
        assert ("turn_on", "switch.self_use_switch") not in ha.calls       # peak shaving clears it
        ha.calls.clear()
        await (s.self_use(T0) if command == "self_use" else getattr(s, command)(T0, 20))
        off, on = (ha.calls.index(("turn_off", "switch.peak_shaving_switch")),
                   ha.calls.index(("turn_on", "switch.self_use_switch")))
        assert off < on, (command, ha.calls)


async def test_without_the_self_use_switch_nothing_changes():
    ha = HA()
    s = Solis(ha, {"tou_switch": "switch.tou", "peak_shaving_switch": "switch.shave"}, method="force")
    await s.peak_shave(T0, 2.5, 10)
    await s.self_use(T0)
    assert all("self_use" not in str(eid) for _, eid in ha.calls)


async def test_the_self_use_switch_comes_back_on_when_peak_shaving_ends(tmp_path):
    app, fake = await winter_app(tmp_path, self_use_switch=True)
    assert app.solis.entities["self_use_switch"] == SELF_USE
    assert fake.s[SELF_USE]["state"] == "off"            # cleared by the integration when peak shaving went on
    fake.soc = 60.0                                      # well above the band: self-use
    await run(app, fake, fake.now + timedelta(minutes=20))
    assert app.solis.mode == "self_use"
    assert fake.s[SHAVE]["state"] == "off" and fake.s[SELF_USE]["state"] == "on"
    assert not drift_events(app)                         # Battery Pilot's own changes are not reported


def test_the_self_use_switch_is_found_by_its_name():
    states = {"switch.inverter_self_use_mode": {"state": "on"},
              "switch.inverter_self_use_mode_backup": {"state": "on"}}
    found, _ = discovery.resolve_all(Options(), {**FakeHA(T0).s, **states})
    assert found["self_use_switch"].entity_id == "switch.inverter_self_use_mode"
