# --- Stage 1: Get Valkey Binaries ---
FROM valkey/valkey:8 AS valkey_source

# --- Stage 2: Builder & Runtime ---
FROM ghcr.io/astral-sh/uv:trixie-slim

ENV PYTHONUNBUFFERED=1
ENV VALKEY_URL="redis://127.0.0.1:6379/0"
ENV DB_PATH="/app_data/data.db"
# Use a custom venv location for consistency
ENV UV_PROJECT_ENVIRONMENT="/venv"
ENV PATH="/venv/bin:$PATH"

WORKDIR /app

# Install Runtime Dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    supervisor \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy Valkey binaries
COPY --from=valkey_source /usr/local/bin/valkey-server /usr/local/bin/
COPY --from=valkey_source /usr/local/bin/valkey-cli /usr/local/bin/

# Setup directories
RUN mkdir -p /app_data/valkey && \
    mkdir -p /var/log/supervisor

# Install Python & Dependencies
COPY pyproject.toml uv.lock .python-version ./
RUN uv python install
# Install ONLY main dependencies (no dev/watchfiles)
RUN uv sync --no-install-project --no-dev

# Copy Application Code
COPY src ./src
COPY static ./static
COPY migrations ./migrations
COPY alembic.ini .
COPY docker/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Install project
RUN uv sync --no-dev

EXPOSE 8000
VOLUME ["/app_data"]

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
