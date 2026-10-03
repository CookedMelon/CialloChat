ARG TARGETARCH
FROM python:3.12-slim-bookworm@sha256:9901e0a8d75037d8242ed43155cbcb2d1f61be1356383d8054afb59fd50e39c4 AS base-amd64
FROM python:3.12-slim-bookworm@sha256:349275ed26e7aea20752e1fd63c40c30a0281d324e51fb41612c2433cb6482d4 AS base-arm64
FROM base-${TARGETARCH} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --only-binary=:all: -r /app/requirements.lock
COPY src/streamctl/authserver.py /app/authserver.py
COPY src/streamctl/watchdog.py /app/watchdog.py
USER 65532:65532
ENTRYPOINT ["python", "/app/authserver.py"]
