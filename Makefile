.PHONY: all up run stop clean logs run-prod

# Default target
all: run

# Start infrastructure (Valkey) in detached mode
up:
	docker compose up -d valkey

# Start the full stack (App + Valkey) with live rebuilds
run:
	docker compose up --build

# Run the production image from registry
# - Pulls latest image
# - Maps host port 14123 to container port 8000
# - --rm ensures container and anonymous volumes are deleted on exit
run-prod:
	docker pull ghcr.io/mayurifag/tg-ai-manager:latest
	docker run --rm -it \
		-p 14123:8000 \
		--name tg_ai_manager_prod \
		ghcr.io/mayurifag/tg-ai-manager:latest

# Stop all containers
stop:
	docker compose down

# View logs from docker containers
logs:
	docker compose logs -f

# Clean up artifacts (optional)
clean:
	rm -rf __pycache__
	rm -rf cache/*
