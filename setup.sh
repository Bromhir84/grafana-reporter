#!/bin/bash
set -e

echo "Checking repository..."

if [ ! -d "/app/repo/.git" ]; then
    echo "Cloning repository..."
    git clone -q https://github.com/Bromhir84/grafana-reporter.git /app/repo
else
    echo "Updating repository..."
    cd /app/repo
    git pull -q
fi

echo "Repository ready."

echo "Starting application..."
cd /app/repo
chmod +x start.sh
exec ./start.sh