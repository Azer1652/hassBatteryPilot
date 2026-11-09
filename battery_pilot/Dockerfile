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

CMD ["python3", "-m", "pilot"]
