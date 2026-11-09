# Build-only sources do not remain in the runtime image.
FROM valkey/valkey:8.1 AS valkey_source
FROM ghcr.io/astral-sh/uv:trixie-slim AS python_builder
ENV UV_PROJECT_ENVIRONMENT="/venv"
WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
RUN uv python install && uv sync --locked --no-install-project --no-dev --group container

FROM debian:trixie-slim
ENV PYTHONUNBUFFERED=1
ENV VALKEY_URL="redis://127.0.0.1:6379/0"
ENV DB_PATH="/app_data/data.db"
ENV PATH="/venv/bin:$PATH"
WORKDIR /app

COPY --from=python_builder /root/.local/share/uv/python /root/.local/share/uv/python
COPY --from=python_builder /venv /venv

# Bootstrap Debian HTTPS with the locked certifi bundle before installing system CAs.
# Valkey requires libsystemd and OpenSSL; certificates enable outbound TLS.
# The application does not mount filesystems; do not grant mount helpers setuid privilege.
RUN ca_bundle="$(/venv/bin/python -m certifi)" \
    && test -s "$ca_bundle" \
    && sed -i 's|http://deb.debian.org/|https://deb.debian.org/|g' /etc/apt/sources.list.d/debian.sources \
    && grep -q '^URIs: https://deb.debian.org/' /etc/apt/sources.list.d/debian.sources \
    && ! grep -q '^URIs:.*http://' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=0 -o "Acquire::https::CAInfo=$ca_bundle" -o APT::Update::Error-Mode=any update \
    && apt-get -o Acquire::Retries=0 -o "Acquire::https::CAInfo=$ca_bundle" upgrade -y \
    && apt-get -o Acquire::Retries=0 -o "Acquire::https::CAInfo=$ca_bundle" install -y --no-install-recommends \
        ca-certificates \
        libsystemd0 \
    && rm -rf /var/lib/apt/lists/* \
    && chmod u-s /usr/bin/mount /usr/bin/umount

COPY --from=valkey_source /usr/local/bin/valkey-server /usr/local/bin/
COPY --from=valkey_source /usr/local/bin/valkey-cli /usr/local/bin/

RUN mkdir -p /app_data/valkey && \
    mkdir -p /var/log/supervisor

COPY src ./src
COPY static ./static
COPY migrations ./migrations
COPY alembic.ini .
COPY docker/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
VOLUME ["/app_data"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD ["/venv/bin/python", "-c", "import http.client; c = http.client.HTTPConnection('127.0.0.1', 8000, timeout=5); c.request('GET', '/api/live'); raise SystemExit(c.getresponse().status >= 400)"]

ENTRYPOINT ["/entrypoint.sh"]
CMD ["/venv/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
