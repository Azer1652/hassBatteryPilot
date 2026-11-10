"""The main loop, the web endpoints and the entry point."""
import asyncio
import os
from datetime import timedelta

import pytest
from fake_ha import FakeHA, utc

from pilot import __main__ as entry
from pilot.app import App
from pilot.config import Options
from pilot.web import INGRESS_IP, default_allowed, make_web


@pytest.fixture
def tmpdata(tmp_path):
    return str(tmp_path / "data")


def fast_sleep(fake, stop, until):
    """Replace the loop's sleep: fake time passes instead, and the loop stops at `until`."""
    async def sleep(event, seconds):
        fake.advance(timedelta(seconds=seconds))
        if fake.now >= until:
            stop.set()
    return sleep


async def test_the_loop_waits_for_home_assistant_then_runs_and_stops_cleanly(tmpdata, monkeypatch):
    fake = FakeHA(utc(2026, 1, 14, 23, 0), soc=20)
    fake.s["input_boolean.battery_pilot_control"]["state"] = "on"
    fake.ping_error = "Cannot connect to host"
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    stop = asyncio.Event()
    real_ping = fake.ping

    async def ping():
        if fake.now >= utc(2026, 1, 14, 23, 1):
            fake.ping_error = None                               # HA is up after a minute
        return await real_ping()

    fake.ping = ping
    monkeypatch.setattr("pilot.app._sleep", fast_sleep(fake, stop, utc(2026, 1, 15, 1, 0)))
    await app.run(stop)
    assert app.ready
    assert len([e for e in app.events if e["text"].startswith("Waiting for Home Assistant")]) == 1
    assert os.path.exists(os.path.join(tmpdata, "state.json"))  # saved at shutdown
    assert fake.s["switch.solis_time_of_use"]["state"] == "off"  # and the inverter left in self-use


async def test_the_loop_retries_setup_until_a_missing_entity_appears(tmpdata, monkeypatch):
    fake = FakeHA(utc(2026, 1, 14, 12, 0))
    app = App(Options(entities={"battery_soc": "sensor.my_battery_soc"}), fake, data_dir=tmpdata,
              clock=lambda: fake.now)                            # set to an entity that does not exist (yet)
    stop = asyncio.Event()
    inner = fast_sleep(fake, stop, utc(2026, 1, 14, 12, 30))

    async def sleep(event, seconds):
        if fake.now >= utc(2026, 1, 14, 12, 10):                 # it appears (e.g. an integration came back)
            fake.s["sensor.my_battery_soc"] = fake.s["sensor.solis_battery_soc"]
        await inner(event, seconds)

    monkeypatch.setattr("pilot.app._sleep", sleep)
    await app.run(stop)
    assert any(e["text"].startswith("Required entities are missing") for e in app.events)
    assert app.ready and app.problem is None


async def test_a_recurring_error_is_logged_once_per_five_minutes(tmpdata, monkeypatch):
    fake = FakeHA(utc(2026, 1, 14, 12, 0))
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    stop = asyncio.Event()
    monkeypatch.setattr("pilot.app._sleep", fast_sleep(fake, stop, utc(2026, 1, 14, 12, 11)))

    async def broken():
        raise RuntimeError("boom")

    assert await app.setup()
    app.step = broken
    await app.run(stop)
    assert len([e for e in app.events if e["text"] == "Error: RuntimeError: boom"]) == 3   # 0, 5 and 10 minutes


# ---------------------------------------------------------------- web
async def test_the_page_and_the_status_are_served(tmpdata, aiohttp_client):
    fake = FakeHA(utc(2026, 1, 14, 12, 0))
    app = App(Options(), fake, data_dir=tmpdata, clock=lambda: fake.now)
    assert await app.setup()
    await app.step()
    client = await aiohttp_client(make_web(app, None))
    r = await client.get("/")
    assert r.status == 200 and r.headers["Cache-Control"] == "no-cache" and "Battery Pilot" in await r.text()
    r = await client.get("/api/status")
    s = await r.json()
    assert r.status == 200 and s["plan"]["steps"] and s["readings"]["soc"] is not None


def test_only_the_ingress_proxy_may_connect_unless_told_otherwise(monkeypatch):
    monkeypatch.delenv("BP_ALLOW_ANY_IP", raising=False)
    assert INGRESS_IP in default_allowed()
    monkeypatch.setenv("BP_ALLOW_ANY_IP", "1")
    assert default_allowed() is None


# ---------------------------------------------------------------- entry point
@pytest.mark.parametrize("content", ["{broken", "[]", '{"contract": "monthly"}'])
async def test_bad_settings_stop_the_app_with_a_message(tmp_path, monkeypatch, caplog, content):
    options = tmp_path / "options.json"
    options.write_text(content)
    monkeypatch.setenv("BP_OPTIONS", str(options))
    assert await entry.main() == 1                               # before anything touches the network
    assert any("Settings problem" in r.getMessage() for r in caplog.records)


def test_the_home_assistant_config_folder_is_found(tmp_path, monkeypatch):
    (tmp_path / "configuration.yaml").write_text("homeassistant:\n")
    monkeypatch.setenv("BP_CONFIG_DIR", str(tmp_path))
    assert entry.config_dir() == str(tmp_path)
