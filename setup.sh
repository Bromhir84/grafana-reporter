#!/bin/bash
set -e  # Exit on error

# Clone or update repo
if [ ! -d "/app/.git" ]; then
    echo "Cloning repository..."
    git clone https://github.com/Bromhir84/grafana-reporter.git /app/repo
else
    echo "Updating repository..."
    cd /app/repo
    git pull
fi

chmod +x /app/repo/start.sh
exec /app/repo/start.sh