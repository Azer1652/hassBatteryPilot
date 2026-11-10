"""The dashboard panel in the HA sidebar (ingress) and its JSON endpoint."""
from __future__ import annotations

import os
import re
from datetime import date

from aiohttp import web

STATIC = os.path.join(os.path.dirname(__file__), "static")
INGRESS_IP = "172.30.32.2"   # the Supervisor's ingress proxy
APP_KEY = web.AppKey("pilot_app", object)
ALLOWED_KEY = web.AppKey("allowed_ips", object)
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@web.middleware
async def only_ingress(request: web.Request, handler):
    allowed = request.app.get(ALLOWED_KEY)
    if allowed is not None and request.remote not in allowed:
        raise web.HTTPForbidden(text="Battery Pilot is only reachable through Home Assistant.")
    return await handler(request)


async def index(request: web.Request):
    return web.FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})


async def status(request: web.Request):
    return web.json_response(request.app[APP_KEY].status(), dumps=_dumps)


async def health(request: web.Request):
    """The Docker HEALTHCHECK: 503 when the main loop has stalled, so the Supervisor restarts the app."""
    if request.app[APP_KEY].healthy():
        return web.Response(text="ok")
    raise web.HTTPServiceUnavailable(text="the main loop has stalled")


async def history_index(request: web.Request):
    return web.json_response(request.app[APP_KEY].history_index(), dumps=_dumps)


async def history_day(request: web.Request):
    raw = request.match_info["day"]
    try:
        day = date.fromisoformat(raw) if DAY_RE.match(raw) else None
    except ValueError:
        day = None
    doc = request.app[APP_KEY].history_day(day) if day else None
    if doc is None:
        raise web.HTTPNotFound(text="No history for that day.")
    return web.json_response(doc, dumps=_dumps)


def _dumps(obj):
    import json
    return json.dumps(obj, default=str)


def make_web(app, allowed_ips: set[str] | None = None) -> web.Application:
    w = web.Application(middlewares=[only_ingress])
    w[APP_KEY] = app
    w[ALLOWED_KEY] = allowed_ips
    w.router.add_get("/", index)
    w.router.add_get("/api/status", status)
    w.router.add_get("/health", health)
    w.router.add_get("/api/history", history_index)
    w.router.add_get("/api/history/{day}", history_day)
    w.router.add_static("/static/", STATIC)
    return w


def default_allowed() -> set[str] | None:
    if os.environ.get("BP_ALLOW_ANY_IP"):
        return None
    return {INGRESS_IP, "127.0.0.1", "::1"}
