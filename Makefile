.PHONY: all up run stop clean logs

# Default target
all: run

# Start infrastructure (Valkey) in detached mode
up:
	docker compose up -d valkey

# Start infrastructure and run the application
# Depends on 'up' to ensure DB is ready
run: up
	uv run hypercorn src.app:app --reload --bind 127.0.0.1:8000

# Stop all containers
stop:
	docker compose down

# View logs from docker containers
logs:
	docker compose logs -f

# Clean up artifacts (optional)
clean:
	rm -rf __pycache__
	rm -rf .pytest_cache
