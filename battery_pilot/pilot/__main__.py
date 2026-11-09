"""Start Battery Pilot: `python3 -m pilot` inside the app container."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from aiohttp import web

from .app import App
from .config import ConfigError, load
from .ha import HA
from .web import default_allowed, make_web

VERSION = os.environ.get("BP_VERSION", "dev")


def config_dir() -> str | None:
    for d in (os.environ.get("BP_CONFIG_DIR"), "/homeassistant", "/config"):
        if d and os.path.isfile(os.path.join(d, "configuration.yaml")):
            return d
    return None


async def main() -> int:
    logging.basicConfig(level=os.environ.get("BP_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    log = logging.getLogger("battery_pilot")
    try:
        opts = load(os.environ.get("BP_OPTIONS", "/data/options.json"))
    except ConfigError as e:
        log.error("Settings problem: %s", e)
        return 1
    ha = HA(os.environ.get("BP_API", "http://supervisor/core/api"),
            os.environ.get("BP_WS", "ws://supervisor/core/websocket"),
            os.environ.get("SUPERVISOR_TOKEN", ""))
    app = App(opts, ha, data_dir=os.environ.get("BP_DATA", "/data"), config_dir=config_dir(), version=VERSION)

    runner = web.AppRunner(make_web(app, default_allowed()), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("BP_PORT", "8099"))).start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        await app.run(stop)
    finally:
        await runner.cleanup()
        await ha.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
