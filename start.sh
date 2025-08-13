#!/bin/bash
set -e

echo "Installing Python dependencies..."
pip install --user --no-cache-dir -q -r /app/repo/requirements.txt
echo "Dependencies installed."

echo "Starting server..."
cd /app/repo
python3 -m uvicorn report_api:app --host 0.0.0.0 --port 8000