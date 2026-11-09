"""The dashboard panel in the HA sidebar (ingress) and its JSON endpoint."""
from __future__ import annotations

import os

from aiohttp import web

STATIC = os.path.join(os.path.dirname(__file__), "static")
INGRESS_IP = "172.30.32.2"   # the Supervisor's ingress proxy
APP_KEY = web.AppKey("pilot_app", object)


@web.middleware
async def only_ingress(request: web.Request, handler):
    allowed = request.app.get("allowed_ips")
    if allowed is not None and request.remote not in allowed:
        raise web.HTTPForbidden(text="Battery Pilot is only reachable through Home Assistant.")
    return await handler(request)


async def index(request: web.Request):
    return web.FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})


async def status(request: web.Request):
    return web.json_response(request.app[APP_KEY].status(), dumps=_dumps)


def _dumps(obj):
    import json
    return json.dumps(obj, default=str)


def make_web(app, allowed_ips: set[str] | None = None) -> web.Application:
    w = web.Application(middlewares=[only_ingress])
    w[APP_KEY] = app
    w["allowed_ips"] = allowed_ips
    w.router.add_get("/", index)
    w.router.add_get("/api/status", status)
    w.router.add_static("/static/", STATIC)
    return w


def default_allowed() -> set[str] | None:
    if os.environ.get("BP_ALLOW_ANY_IP"):
        return None
    return {INGRESS_IP, "127.0.0.1", "::1"}
