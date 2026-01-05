FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1
# Install packages into system python (no venv needed for docker container)
ENV UV_PROJECT_ENVIRONMENT="/usr/local"

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy configuration
COPY pyproject.toml .

# Install dependencies
RUN uv sync --no-install-project

# Source code is mounted at runtime via compose
CMD ["uv", "run", "hypercorn", "src.app:app", "--reload", "--bind", "0.0.0.0:8000"]
