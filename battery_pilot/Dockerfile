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

# The base image's /init (s6-overlay) clears the environment before it runs CMD;
# with-contenv puts it back (SUPERVISOR_TOKEN, BP_VERSION, ...).
CMD ["/command/with-contenv", "python3", "-m", "pilot"]
