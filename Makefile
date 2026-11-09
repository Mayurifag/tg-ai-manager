.PHONY: all up up-prod down logs clean ci format

# Default target
all: up

# Start the full stack (App + Valkey) with live rebuilds
up:
	docker compose up --build

# Run the production image from registry
# - Pulls latest image
# - Loopback-only port and persistent named volume for account data
up-prod:
	docker pull ghcr.io/mayurifag/tg-ai-manager:latest
	docker run --rm -it \
		-p 127.0.0.1:14123:8000 \
		--mount type=volume,src=tg_ai_manager_prod_data,dst=/app_data \
		--env-file .env \
		--name tg_ai_manager_prod \
		ghcr.io/mayurifag/tg-ai-manager:latest

# Stop all containers
down:
	docker compose down --remove-orphans

# View logs from docker containers
logs:
	docker compose logs -f

# Clean up artifacts (optional)
clean:
	rm -rf __pycache__

ci:
	uv run --no-sync ruff format --check
	uv run --no-sync ruff check
	node --version > /dev/null
	for file in static/js/*.js scripts/*.cjs; do node --check "$$file" || exit $$?; done
	node scripts/generate-emoji-map.cjs --check

format:
	uv run --no-sync ruff format
	uv run --no-sync ruff check --fix
