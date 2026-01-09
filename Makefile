.PHONY: all up up-prod down logs clean

# Default target
all: up

# Start the full stack (App + Valkey) with live rebuilds
up:
	docker compose up --build

# Run the production image from registry
# - Pulls latest image
# - Maps host port 14123 to container port 8000
# - --rm ensures container and anonymous volumes are deleted on exit
up-prod:
	docker pull ghcr.io/mayurifag/tg-ai-manager:latest
	docker run --rm -it \
		-p 14123:8000 \
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
	rm -rf cache/*
