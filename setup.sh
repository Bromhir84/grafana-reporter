#!/bin/bash
set -e

echo "Checking repository..."

# Clone or update repo
if [ ! -d "/app/repo/.git" ]; then
    echo "Cloning repository..."
    git clone -q https://github.com/Bromhir84/grafana-reporter.git /app/repo
else
    echo "Updating repository..."
    cd /app/repo
    git pull -q
fi

echo "Repository ready."

# Ensure local pip bin is in PATH
export PATH="$HOME/.local/bin:$PATH"

echo "Starting application..."
cd /app/repo
chmod +x start.sh
exec ./start.sh