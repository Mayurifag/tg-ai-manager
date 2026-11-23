FROM ghcr.io/astral-sh/uv:0.13.9-trixie-slim@sha256:9b7790faef7a3f2b9ba8f2f7c48b959c1359fe2bd51c44804b6ca61766fbacdf

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# 1. Store the virtual environment OUTSIDE /app
# This prevents the docker-compose volume mount (.:/app) from hiding/deleting it
ENV UV_PROJECT_ENVIRONMENT="/venv"

# Copy config
COPY pyproject.toml uv.lock .python-version ./

# 2. Install Python & Dependencies
RUN uv python install
# Install dependencies
# We remove --frozen so it auto-updates lockfile if you changed pyproject.toml
RUN uv sync --no-install-project

# 3. Add venv to PATH so we can run 'hypercorn' directly
ENV PATH="/venv/bin:$PATH"

# Source code is mounted at runtime via compose
CMD ["hypercorn", "src.app:app", "--reload", "--bind", "0.0.0.0:8000"]
