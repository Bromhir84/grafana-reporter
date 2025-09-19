#!/bin/bash
set -e

echo "Installing Python dependencies..."
pip install --user --no-cache-dir -q -r /app/repo/requirements.txt
echo "Dependencies installed."

# Ensure local pip bin is in PATH
export PATH="$HOME/.local/bin:$PATH"
echo "Starting server..."
cd /app/repo

# Start uvicorn as the main process
exec uvicorn repo.main:app --host 0.0.0.0 --port 8000