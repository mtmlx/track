FROM mcr.microsoft.com/playwright/python:v1.61.0-resolute@sha256:ac650e6fadc8d551a602031cb27a2d19b91c9eef14fbe250850c297e4aa9cd21

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY pyproject.toml README.md requirements.lock build-requirements.lock /app/
COPY src /app/src
COPY scripts /app/scripts

RUN set -eux; \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get upgrade -y && \
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
    rm -rf /var/lib/apt/lists/* && \
    python3 -m pip install --require-hashes -r /app/requirements.lock -r /app/build-requirements.lock && \
    python3 -m pip install --no-deps --no-build-isolation /app && \
    useradd --create-home --shell /usr/sbin/nologin appuser && \
    chown -R appuser:appuser /app && \
    chmod +x /app/scripts/run_sync.sh /app/scripts/run_sync_loop.sh

EXPOSE 10000

USER appuser

CMD ["shipment-api"]
