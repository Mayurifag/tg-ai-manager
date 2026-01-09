#!/bin/bash
set -e

# Ensure the directory for Valkey persistence exists
# This is necessary because mounting a volume at /app_data hides the build-time directory
if [ ! -d "/app_data/valkey" ]; then
    echo "Creating /app_data/valkey directory..."
    mkdir -p /app_data/valkey
fi

# Execute the CMD passed to the docker container (supervisord)
exec "$@"