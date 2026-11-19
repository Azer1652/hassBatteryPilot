"""The settings form of the page's Settings tab (pilot/form.py, GET /api/settings/form)."""
import asyncio
import os
import re
import subprocess
import sys

import yaml
from test_settings import SAVED, client_for

from pilot import form, settings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "battery_pilot", "config.yaml"), encoding="utf-8") as _f:
    SCHEMA = yaml.safe_load(_f)["schema"]


def test_every_setting_is_in_the_form_once():
    keys = form.all_fields()
    assert len(keys) == len(set(keys))
    assert set(keys) == set(settings.KNOWN) == set(SCHEMA)


def test_the_choices_and_patterns_are_those_of_the_add_on_schema():
    lists = {k: v[5:-1].split("|") for k, v in SCHEMA.items() if v.startswith("list(")}
    assert form.CHOICES == lists
    patterns = {k: v[6:-1] for k, v in SCHEMA.items() if v.startswith("match(")}
    assert form.PATTERNS == patterns
    optional_numbers = {k for k, v in SCHEMA.items() if v.startswith("int(") and v.endswith("?")}
    assert settings.OPTIONAL_NUMBER == optional_numbers


def test_kinds_follow_the_schema():
    kind = {"float": "float", "int": "int", "bool": "bool", "list": "choice", "match": "text"}
    for k, v in SCHEMA.items():
        f = form.field(k)
        if v == "str?":
            assert f["kind"] in ("entity", "text") and f["optional"], k
        else:
            assert f["kind"] == kind[re.match(r"[a-z]+", v).group()], k


def test_solis_and_marstek_fields_are_only_for_their_battery():
    solis = next(g for g in form.GROUPS if g["id"] == "solis")
    marstek = next(g for g in form.GROUPS if g["id"] == "marstek")
    assert solis["show"] == {"battery_system": ["solis"]} and marstek["show"] == {"battery_system": ["marstek"]}
    from pilot.discovery import MARSTEK_ONLY, SOLIS_ONLY
    for k in SOLIS_ONLY:
        assert form.field(k)["show"] == {"battery_system": ["solis"]}
    for k in MARSTEK_ONLY:          # (grid_power and the meter's averages may be set for a Solis too)
        assert form.field(k).get("show") in (None, {"battery_system": ["marstek"]})


def test_each_contract_has_its_group():
    for c in form.CHOICES["contract"]:
        g = next(g for g in form.GROUPS if g["id"] == c)
        assert g["show"] == {"contract": [c]} and g["other"]
        assert all(k.startswith(c + "_") for k in g["fields"])


def test_the_text_file_matches_the_configuration_tab():
    out = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "settings_text.py"), "--check"],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout
    text = form.texts()
    assert set(form.all_fields()) <= set(text)


async def test_the_form_gives_the_saved_values_and_every_field(tmp_path, aiohttp_client):
    client, _, _ = await client_for(tmp_path, aiohttp_client, saved={**SAVED, "pv_power": "sensor.pv"})
    d = await (await client.get("/api/settings/form")).json()
    assert [g["id"] for g in d["groups"]][:2] == ["contract", "fixed"]
    assert d["values"]["contract"] == "fixed" and d["values"]["pv_power"] == "sensor.pv"
    assert d["fields"]["fixed_day_rate"] == {"kind": "float", "min": 0, "max": 100, "optional": False,
                                             "default": 18.59, **form.texts()["fixed_day_rate"]}
    assert d["fields"]["pv_power"]["kind"] == "entity"


async def test_saving_from_the_form_then_reading_it_back_shows_the_saved_values(tmp_path, aiohttp_client):
    client, app, fake = await client_for(tmp_path, aiohttp_client)
    r = await client.post("/api/settings", json={"contract": "dynamic", "solis_slave": 2})
    assert r.status == 200 and len((await r.json())["changes"]) == 2
    d = await (await client.get("/api/settings/form")).json()      # (options.json changes only at a start)
    assert d["values"]["contract"] == "dynamic" and d["values"]["solis_slave"] == 2
    assert app.o.contract == "fixed"                                # the running settings stay until a restart
    r = await client.post("/api/settings", json={"solis_slave": None})      # emptied: left out
    assert (await r.json())["changes"] == [{"key": "solis_slave", "old": 2, "new": None}]
    assert "solis_slave" not in fake.supervisor_calls[-1][2]["options"]


async def test_the_restart_button_asks_the_supervisor(tmp_path, aiohttp_client):
    client, app, fake = await client_for(tmp_path, aiohttp_client)
    app.restart = lambda real=app.restart: real(delay_s=0)
    r = await client.post("/api/restart")
    assert r.status == 200
    await app._restart_task
    assert fake.supervisor_calls[-1][:2] == ("POST", "/addons/self/restart")


async def test_a_refused_restart_is_logged(tmp_path, aiohttp_client):
    client, app, fake = await client_for(tmp_path, aiohttp_client)
    fake.supervisor_error = "403"
    await app.restart(delay_s=0)
    assert any("did not restart" in e["text"] for e in app.events)
    await asyncio.sleep(0)
