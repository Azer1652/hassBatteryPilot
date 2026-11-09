ARG BUILD_FROM
FROM $BUILD_FROM

ARG BUILD_VERSION
ENV BP_VERSION=${BUILD_VERSION}

RUN apk add --no-cache python3

COPY run.py /app/run.py
COPY templates /app/templates

CMD ["python3", "/app/run.py"]
