#!/bin/bash
set -e  # Exit on error

# Clone or update repo
if [ ! -d "/app/.git" ]; then
    echo "Cloning repository..."
    rm -rf /app/*
    git clone https://github.com/Bromhir84/grafana-reporter.git /app
else
    echo "Updating repository..."
    cd /app
    git pull
fi

chmod +x /app/start.sh
exec /app/start.sh