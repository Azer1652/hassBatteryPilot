"""The "force peak shaving" switch: peak shaving, whatever the plan says, until it is turned off by hand."""
from datetime import datetime, timedelta, timezone

from fake_ha import TZ, FakeHA, utc

from pilot.app import App
from pilot.config import Options
from pilot.controller import Readings, decide, expected_modes
from pilot.planner import Step

T0 = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)
CONTROL = "input_boolean.battery_pilot_control"
FORCE = "input_boolean.battery_pilot_force_peak_shaving"


def step(action, soc_end=30):
    return Step(T0.replace(minute=0), 1.0, action, 30, soc_end, 0, 0, 0, 0, 27, 5, 0)


def rd(load=0.6, pv=0.0, soc=60):
    return Readings(T0, T0.astimezone(TZ), soc, load, pv, load, load, 51.2)


def test_the_switch_beats_the_plan_the_fill_switch_and_no_plan():
    o = Options()
    for action in ("self_use", "charge", "discharge", "solar_hold"):
        d = decide(step(action, 80), rd(), o, "self_use", "", 0, True, can_shave=True, force_shave=True)
        assert d.command == "peak_shave" and "force peak shaving" in d.reason, action
    assert decide(None, rd(), o, "charge", "", 10, True, can_shave=True, force_shave=True).command == "peak_shave"
    assert decide(step("charge", 90), rd(soc=20), o, "self_use", "", 0, True, can_shave=True, fill=True,
                  force_shave=True).command == "peak_shave"
    assert decide(step("discharge"), rd(), o, "peak_shave", "", 0, True, can_shave=True,
                  force_shave=True).command == "none"                      # stays there


def test_without_control_or_the_inverter_switch_it_does_nothing():
    o = Options()
    assert decide(step("charge", 90), rd(), o, "self_use", "", 0, False, can_shave=True,
                  force_shave=True).command == "none"
    d = decide(step("charge", 90), rd(), o, "self_use", "", 0, True, can_shave=False, force_shave=True)
    assert d.command != "peak_shave"


def test_the_chart_shows_peak_shaving_while_it_is_on():
    steps = [Step(T0 + timedelta(hours=i), 1.0, a, 50, 50, 0, 0, 0.6, 0, 27, 5, 0)
             for i, a in enumerate(("charge", "self_use", "discharge"))]
    modes = expected_modes(steps, Options(), TZ, "self_use", True, force_shave=True)
    assert [m for m, _ in modes] == ["peak_shave"] * 3
    assert [m for m, _ in expected_modes(steps, Options(), TZ, "self_use", True)][0] != "peak_shave"


async def run(app, fake, minutes):
    end = fake.now + timedelta(minutes=minutes)
    while fake.now < end:
        await app.step()
        fake.advance(timedelta(seconds=30))


async def test_the_inverter_stays_in_peak_shaving_until_the_switch_is_off(tmp_path):
    fake = FakeHA(utc(2026, 1, 14, 12, 0), soc=70, peak_shaving=True)
    fake.s[CONTROL]["state"] = "on"
    app = App(Options(), fake, data_dir=str(tmp_path / "d"), clock=lambda: fake.now)
    assert await app.setup()
    await run(app, fake, 10)
    fake.s[FORCE]["state"] = "on"
    await run(app, fake, 5 * 60)                       # hours, through many plan refreshes
    assert fake.mode() == "peak_shave" and app.force_shave_on
    assert all(s.mode == "peak_shave" for s in app.plan.steps)
    st = app.status()["force_shave"]
    assert st == {"on": True, "can_shave": True, "active": True}
    texts = [e["text"] for e in app.events]
    assert any(t.startswith("Force peak shaving: on.") for t in texts)
    fake.s[FORCE]["state"] = "off"
    await run(app, fake, 2)
    assert any(e["text"].startswith("Force peak shaving: off") for e in app.events)
    assert not app.force_shave_on
    assert app.decision.reason != "peak shaving: forced by the switch"


async def test_the_page_button_turns_the_switch_on_and_off(tmp_path, aiohttp_client):
    from pilot.web import make_web
    fake = FakeHA(utc(2026, 1, 14, 12, 0), soc=70, peak_shaving=True)
    app = App(Options(), fake, data_dir=str(tmp_path / "d"), clock=lambda: fake.now)
    assert await app.setup()
    client = await aiohttp_client(make_web(app, None))
    r = await client.post("/api/force_shave", json={"on": True})
    assert r.status == 200 and fake.s[FORCE]["state"] == "on" and app.force_shave_on
    r = await client.post("/api/force_shave", json={"on": False})
    assert r.status == 200 and fake.s[FORCE]["state"] == "off" and not app.force_shave_on
    assert (await client.post("/api/force_shave", json={"on": "yes"})).status == 400
    outside = await aiohttp_client(make_web(app, {"172.30.32.2"}))
    assert (await outside.post("/api/force_shave", json={"on": True})).status == 403
