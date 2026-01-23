#!/bin/bash
set -e

# Directory for libraries
LIB_DIR="static/libs"
mkdir -p "$LIB_DIR"

echo "Downloading Frontend Libraries..."

# 1. Alpine.js
curl -L -o "$LIB_DIR/alpine.min.js" "https://cdn.jsdelivr.net/npm/alpinejs@3.13.3/dist/cdn.min.js"
echo "✅ Alpine.js downloaded"

# 2. htmx
curl -L -o "$LIB_DIR/htmx.min.js" "https://unpkg.com/htmx.org@1.9.10/dist/htmx.min.js"
echo "✅ htmx downloaded"

# 3. Lottie Web (Standard is fine now, backend handles decompression)
curl -L -o "$LIB_DIR/lottie.min.js" "https://cdnjs.cloudflare.com/ajax/libs/lottie-web/5.12.2/lottie.min.js"
echo "✅ Lottie Web downloaded"

echo "All libraries installed to $LIB_DIR"
