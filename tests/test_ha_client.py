"""The HA client against a small fake of the HA REST and websocket API."""
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp import WSMsgType, web

from pilot.ha import HA, HAError

TOKEN = "secret"
CALLS = web.AppKey("calls", list)


def make_app():
    calls = []
    states = {"sensor.x": {"entity_id": "sensor.x", "state": "12", "attributes": {}}}

    def auth(request):
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            raise web.HTTPUnauthorized()

    async def root(request):
        auth(request)
        return web.json_response({"message": "API running."})

    async def all_states(request):
        auth(request)
        return web.json_response(list(states.values()))

    async def one_state(request):
        auth(request)
        eid = request.match_info["eid"]
        if request.method == "POST":
            body = await request.json()
            states[eid] = {"entity_id": eid, **body}
            return web.json_response(states[eid])
        if eid not in states:
            raise web.HTTPNotFound()
        return web.json_response(states[eid])

    async def service(request):
        auth(request)
        calls.append((request.match_info["d"], request.match_info["s"], await request.json()))
        return web.json_response([])

    async def history(request):
        auth(request)
        t = datetime(2026, 1, 14, 10, tzinfo=timezone.utc)
        return web.json_response([[{"entity_id": "sensor.x", "state": "1", "last_changed": t.isoformat()},
                                   {"state": "2", "last_changed": (t + timedelta(minutes=5)).isoformat()}]])

    async def websocket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        msg = await ws.receive_json()
        if msg.get("access_token") != TOKEN:
            await ws.send_json({"type": "auth_invalid", "message": "bad token"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok"})
        async for m in ws:
            if m.type != WSMsgType.TEXT:
                break
            cmd = m.json()
            if cmd["type"] == "energy/solar_forecast":
                await ws.send_json({"id": cmd["id"], "type": "event", "event": {}})   # noise first
                await ws.send_json({"id": cmd["id"], "type": "result", "success": True,
                                    "result": {"a": {"wh_hours": {}}}})
            else:
                await ws.send_json({"id": cmd["id"], "type": "result", "success": False,
                                    "error": {"code": "unknown_command", "message": "Unknown command."}})
        return ws

    app = web.Application()
    app.router.add_get("/api/", root)
    app.router.add_get("/api/states", all_states)
    app.router.add_route("*", "/api/states/{eid}", one_state)
    app.router.add_post("/api/services/{d}/{s}", service)
    app.router.add_get("/api/history/period/{start}", history)
    app.router.add_get("/api/websocket", websocket)
    app[CALLS] = calls
    return app


@pytest.fixture
async def client(aiohttp_server):
    app = make_app()
    server = await aiohttp_server(app)
    base = str(server.make_url("/api"))
    ha = HA(base, str(server.make_url("/api/websocket")), TOKEN)
    yield ha, app
    await ha.close()


async def test_rest_calls(client):
    ha, app = client
    assert await ha.ping()
    assert (await ha.state("sensor.x"))["state"] == "12"
    assert await ha.state("sensor.nope") is None
    assert "sensor.x" in await ha.states()
    await ha.call("switch", "turn_on", {"entity_id": "switch.y"})
    assert app[CALLS] == [("switch", "turn_on", {"entity_id": "switch.y"})]
    await ha.set_state("sensor.battery_pilot_plan", "charge", {"a": 1})
    assert (await ha.state("sensor.battery_pilot_plan"))["state"] == "charge"


async def test_history_is_parsed(client):
    ha, _ = client
    t = datetime(2026, 1, 14, 10, tzinfo=timezone.utc)
    h = await ha.history(["sensor.x"], t, t + timedelta(hours=1))
    assert [s for _, s in h["sensor.x"]] == ["1", "2"]
    assert h["sensor.x"][1][0] == t + timedelta(minutes=5)


async def test_websocket_commands_and_errors(client):
    ha, _ = client
    res = await ha.ws({"type": "energy/solar_forecast"})
    assert res == [{"a": {"wh_hours": {}}}]
    with pytest.raises(HAError, match="Unknown command"):
        await ha.ws({"type": "nope"})


async def test_wrong_token_is_reported(client):
    ha, _ = client
    ha.token = "wrong"
    assert not await ha.ping()
    assert "HTTP 401" in ha.last_error
    with pytest.raises(HAError):
        await ha.ws({"type": "energy/solar_forecast"})
    ha.token = TOKEN
    assert await ha.ping()
    assert ha.last_error is None


async def test_unreachable_home_assistant_is_reported(unused_tcp_port):
    ha = HA(f"http://127.0.0.1:{unused_tcp_port}/api", f"ws://127.0.0.1:{unused_tcp_port}/api/websocket", TOKEN)
    try:
        assert not await ha.ping()
        assert ha.last_error.startswith("ClientConnectorError")
    finally:
        await ha.close()
