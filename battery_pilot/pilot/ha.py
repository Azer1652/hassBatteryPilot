"""Home Assistant REST and websocket client (through the Supervisor proxy)."""
from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import quote

import aiohttp

from .prices import parse_time

log = logging.getLogger(__name__)


class HAError(Exception):
    pass


class HA:
    def __init__(self, api_url: str, ws_url: str, token: str, session: aiohttp.ClientSession | None = None,
                 supervisor_url: str | None = None):
        self.api = api_url.rstrip("/")
        self.supervisor_url = (supervisor_url or "").rstrip("/")
        self.ws_url = ws_url
        self.token = token
        self._session = session
        self._own_session = session is None
        self.last_error: str | None = None      # why the last ping failed

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._own_session and self._session is not None:
            await self._session.close()

    @property
    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def _request(self, method: str, path: str, body=None, timeout: float = 30, base: str | None = None):
        url = (base or self.api) + path
        async with self.session.request(method, url, json=body, headers=self._headers,
                                        timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status >= 400:
                text = await r.text()
                raise HAError(f"{method} {path}: HTTP {r.status} {text[:200]}")
            if r.content_type == "application/json":
                return await r.json()
            return await r.text()

    async def supervisor(self, method: str, path: str, body=None):
        """A call to the Supervisor itself (the app's own settings), not to Home Assistant."""
        if not self.supervisor_url:
            raise HAError("the app is not running under the Home Assistant Supervisor")
        return await self._request(method, path, body, base=self.supervisor_url)

    # ---------------------------------------------------------------- REST
    async def ping(self) -> bool:
        try:
            await self._request("GET", "/", timeout=10)
        except Exception as e:  # noqa: BLE001 - any failure means "not ready"
            text = str(e)
            self.last_error = text if isinstance(e, HAError) else f"{type(e).__name__}: {text}".rstrip(": ")
            return False
        self.last_error = None
        return True

    async def config(self) -> dict:
        return await self._request("GET", "/config")

    async def states(self) -> dict[str, dict]:
        return {s["entity_id"]: s for s in await self._request("GET", "/states")}

    async def state(self, entity_id: str) -> dict | None:
        try:
            return await self._request("GET", f"/states/{entity_id}")
        except HAError as e:
            if "HTTP 404" in str(e):
                return None
            raise

    async def services(self) -> list:
        return await self._request("GET", "/services")

    async def call(self, domain: str, service: str, data: dict | None = None) -> None:
        await self._request("POST", f"/services/{domain}/{service}", data or {})

    async def set_state(self, entity_id: str, state, attributes: dict | None = None) -> None:
        await self._request("POST", f"/states/{entity_id}", {"state": str(state), "attributes": attributes or {}})

    async def check_config(self) -> tuple[bool, str]:
        res = await self._request("POST", "/config/core/check_config", {}, timeout=180) or {}
        return res.get("result") == "valid", res.get("errors") or ""

    async def history(self, entity_ids: list[str], start: datetime, end: datetime) -> dict[str, list]:
        """{entity_id: [(time, state), ...]} from the recorder."""
        if not entity_ids:
            return {}
        path = (f"/history/period/{quote(start.isoformat())}?end_time={quote(end.isoformat())}"
                f"&filter_entity_id={','.join(entity_ids)}&minimal_response&no_attributes")
        data = await self._request("GET", path, timeout=120) or []
        out: dict[str, list] = {}
        for series in data:
            if not series:
                continue
            eid = series[0].get("entity_id")
            pts = []
            for p in series:
                t = parse_time(p.get("last_changed") or p.get("last_updated"))
                if t is not None:
                    pts.append((t, p.get("state")))
            out[eid] = pts
        return out

    # ---------------------------------------------------------------- websocket
    async def ws(self, *commands: dict) -> list:
        """Run websocket commands in one short connection; returns their results in order."""
        results = []
        async with self.session.ws_connect(self.ws_url, heartbeat=30) as ws:
            msg = await ws.receive_json(timeout=10)
            if msg.get("type") == "auth_required":
                await ws.send_json({"type": "auth", "access_token": self.token})
                msg = await ws.receive_json(timeout=10)
            if msg.get("type") != "auth_ok":
                raise HAError(f"websocket authentication failed: {msg.get('message', msg.get('type'))}")
            for i, cmd in enumerate(commands, start=1):
                await ws.send_json({"id": i, **cmd})
                while True:
                    reply = await ws.receive_json(timeout=60)
                    if reply.get("id") == i and reply.get("type") == "result":
                        break
                if not reply.get("success"):
                    err = reply.get("error") or {}
                    raise HAError(f"{cmd.get('type')}: {err.get('message', 'failed')}")
                results.append(reply.get("result"))
        return results
