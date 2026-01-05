.PHONY: all up run stop clean logs

# Default target
all: run

# Start infrastructure (Valkey) in detached mode
up:
	docker compose up -d valkey

# Start the full stack (App + Valkey) with live rebuilds
run:
	docker compose up --build

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
