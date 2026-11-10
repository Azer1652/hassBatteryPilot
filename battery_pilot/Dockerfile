ARG BUILD_FROM
FROM $BUILD_FROM

ARG BUILD_VERSION
ENV BP_VERSION=${BUILD_VERSION} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Alpine packages only: nothing to compile on a Raspberry Pi.
RUN apk add --no-cache python3 py3-aiohttp tzdata

WORKDIR /app
COPY pilot /app/pilot

# The Supervisor restarts the app (with Watchdog on) when this fails: the web server must answer
# and the main loop must still be going round (see App.healthy). 127.0.0.1 may reach it.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('BP_PORT', '8099') + '/health', timeout=8)"

# The base image's /init (s6-overlay) clears the environment before it runs CMD;
# with-contenv puts it back (SUPERVISOR_TOKEN, BP_VERSION, ...).
CMD ["/command/with-contenv", "python3", "-m", "pilot"]
