FROM python:3.12.16-slim-bookworm@sha256:44cf1793efa7c7a476bb0f2cff81443f4a072c90c1cb127e2924933daa3e8c64 AS dependencies

ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# A dated snapshot fixes package versions while APT still verifies Debian signatures.
ARG DEBIAN_SNAPSHOT=20261126T000000Z
RUN sed -i \
        -e "s|http://deb.debian.org/debian-security|https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|" \
        -e "s|http://deb.debian.org/debian|https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|" \
        -e '/^Types: deb$/a Check-Valid-Until: no' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends libatomic1 tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock

FROM dependencies AS browser
RUN python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /tmp
USER 1000:1000
ENTRYPOINT ["python", "-m", "playwright", "run-server", "--host", "0.0.0.0", "--port", "3000"]

FROM dependencies AS watcher
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY doomsday_checker/ ./doomsday_checker/
RUN python -m pip install --no-cache-dir --no-build-isolation --no-deps . \
    && rm -rf /build/*

WORKDIR /data
USER 1000:1000
ENTRYPOINT ["doomsday-checker", "--config", "/config/config.toml", "--state-path", "/data/state.db", "--bundled-chromium"]
