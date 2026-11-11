"""Export and import of the settings from the page."""
import json

import pytest
import yaml
from fake_ha import FakeHA, utc

from pilot.app import App
from pilot.config import ConfigError, Options
from pilot.settings import prepare
from pilot.web import make_web

with open("battery_pilot/config.yaml", encoding="utf-8") as _f:
    SAVED = yaml.safe_load(_f)["options"]


async def client_for(tmp_path, aiohttp_client, allowed=None, saved=SAVED):
    path = tmp_path / "options.json"
    path.write_text(json.dumps(saved), encoding="utf-8")
    fake = FakeHA(utc(2026, 11, 11, 12, 0))
    app = App(Options(), fake, data_dir=str(tmp_path), clock=lambda: fake.now, version="2.11.0",
              options_path=str(path))
    assert await app.setup()
    return await aiohttp_client(make_web(app, allowed)), app, fake


async def test_export_gives_the_saved_settings(tmp_path, aiohttp_client):
    client, _, _ = await client_for(tmp_path, aiohttp_client)
    doc = await (await client.get("/api/settings")).json()
    assert doc["app"] == "battery_pilot" and doc["version"] == "2.11.0"
    assert doc["options"] == SAVED and doc["exported"].startswith("2026-11-11T12:00")


async def test_check_lists_what_would_change_and_saves_nothing(tmp_path, aiohttp_client):
    client, _, fake = await client_for(tmp_path, aiohttp_client)
    doc = await (await client.get("/api/settings")).json()
    doc["options"]["battery_min_soc"] = 20
    doc["options"]["peak_buffer_pct"] = 10
    doc["options"]["price_sensor"] = "sensor.average_electricity_price"
    doc["options"]["not_a_setting"] = 1
    r = await client.post("/api/settings/check", json=doc)
    assert r.status == 200
    d = await r.json()
    assert d["changes"] == [
        {"key": "battery_min_soc", "old": SAVED["battery_min_soc"], "new": 20},
        {"key": "peak_buffer_pct", "old": 0, "new": 10},
        {"key": "price_sensor", "old": None, "new": "sensor.average_electricity_price"}]
    assert d["ignored"] == ["not_a_setting"] and fake.supervisor_calls == []


async def test_import_saves_through_the_supervisor_and_changes_nothing_that_runs(tmp_path, aiohttp_client):
    client, app, fake = await client_for(tmp_path, aiohttp_client)
    r = await client.post("/api/settings", json={"import_limit_kw": 3, "not_a_setting": 1})   # a bare object
    d = await r.json()
    assert r.status == 200 and d["saved"] and [c["key"] for c in d["changes"]] == ["import_limit_kw"]
    (method, path, body), = fake.supervisor_calls
    assert (method, path) == ("POST", "/addons/self/options")
    assert body["options"] == {**SAVED, "import_limit_kw": 3}        # the other settings keep their value
    assert "not_a_setting" not in body["options"]
    assert app.o.import_limit_kw == Options().import_limit_kw        # used after a restart only
    assert any("Restart the app" in e["text"] for e in app.events)


async def test_an_import_without_changes_saves_nothing(tmp_path, aiohttp_client):
    client, _, fake = await client_for(tmp_path, aiohttp_client)
    d = await (await client.post("/api/settings", json={"options": SAVED})).json()
    assert d == {"saved": False, "changes": [], "ignored": []} and fake.supervisor_calls == []


async def test_an_emptied_entity_name_is_left_out(tmp_path, aiohttp_client):
    saved = {**SAVED, "price_sensor": "sensor.x"}
    client, _, fake = await client_for(tmp_path, aiohttp_client, saved=saved)
    d = await (await client.post("/api/settings", json={"price_sensor": ""})).json()
    assert d["changes"] == [{"key": "price_sensor", "old": "sensor.x", "new": None}]
    assert "price_sensor" not in fake.supervisor_calls[0][2]["options"]


@pytest.mark.parametrize("body,why", [
    ({"battery_min_soc": 30, "battery_reserve_soc": 20}, "battery_reserve_soc"),
    ({"import_limit_kw": "a lot"}, "must be a number"),
    ({"peak_buffer_pct": 2.5}, "whole number"),
    ({"contract": "free"}, "contract"),
    ({"something": 1}, "no Battery Pilot settings"),
    ([1, 2], "not a Battery Pilot settings file"),
])
async def test_settings_that_cannot_be_used_are_refused(tmp_path, aiohttp_client, body, why):
    client, _, fake = await client_for(tmp_path, aiohttp_client)
    for path in ("/api/settings/check", "/api/settings"):
        r = await client.post(path, json=body)
        assert r.status == 400 and why in await r.text()
    assert fake.supervisor_calls == []


async def test_text_that_is_not_json_is_refused(tmp_path, aiohttp_client):
    client, _, _ = await client_for(tmp_path, aiohttp_client)
    r = await client.post("/api/settings", data="contract: fixed", headers={"Content-Type": "application/json"})
    assert r.status == 400


async def test_a_refusal_by_the_supervisor_is_shown(tmp_path, aiohttp_client):
    client, _, fake = await client_for(tmp_path, aiohttp_client)
    fake.supervisor_error = "HTTP 403"
    r = await client.post("/api/settings", json={"import_limit_kw": 3})
    assert r.status == 502 and "HTTP 403" in await r.text()


async def test_settings_are_only_reachable_through_home_assistant(tmp_path, aiohttp_client):
    client, _, fake = await client_for(tmp_path, aiohttp_client, allowed={"172.30.32.2"})
    assert (await client.get("/api/settings")).status == 403
    assert (await client.post("/api/settings", json={"import_limit_kw": 3})).status == 403
    assert fake.supervisor_calls == []


def test_a_whole_number_for_a_decimal_setting_is_fine():
    merged, changes, _ = prepare({"import_limit_kw": 3}, dict(SAVED))
    assert merged["import_limit_kw"] == 3 and len(changes) == 1
    with pytest.raises(ConfigError):
        prepare({"winter_night_charge": "yes"} if isinstance(SAVED.get("winter_night_charge"), bool)
                else {"peak_buffer_pct": True}, dict(SAVED))
