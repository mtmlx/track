FROM mcr.microsoft.com/playwright/python:v1.62.0-resolute@sha256:e0786fd1d24f9950d9fb6c36e713a04c5671bf79d0b36e2a94b31b5688c01fee

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH="/opt/track-venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml README.md requirements.lock build-requirements.lock requirements-os.lock /app/
COPY src /app/src
COPY scripts /app/scripts

RUN set -eux; \
    apt-get update && \
    for package in \
        openssh-client \
        curl \
        libcurl4t64 \
        libcurl3t64-gnutls \
        ffmpeg \
        libavcodec62 \
        libavfilter11 \
        libavformat62 \
        libavutil60 \
        libswresample6 \
        libswscale9; do \
        if dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null | grep -qx installed; then \
            apt-get purge --auto-remove -y "$package"; \
        fi; \
    done; \
    sed '/^#/d' /app/requirements-os.lock | xargs apt-get install --only-upgrade --no-install-recommends -y && \
    rm -rf /var/lib/apt/lists/* && \
    python3 -m venv /opt/track-venv && \
    python3 -m pip install --require-hashes -r /app/requirements.lock -r /app/build-requirements.lock && \
    python3 -m pip install --no-deps --no-build-isolation /app && \
    useradd --create-home --shell /usr/sbin/nologin appuser && \
    chown -R appuser:appuser /app && \
    chmod +x /app/scripts/run_sync.sh /app/scripts/run_sync_loop.sh

EXPOSE 10000

USER appuser

CMD ["shipment-api"]
