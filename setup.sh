#!/bin/bash
set -e

# Clone or update repo
if [ ! -d "/app/repo/.git" ]; then
    echo "Cloning repository..."
    git clone https://github.com/Bromhir84/grafana-reporter.git /app/repo
else
    echo "Updating repository..."
    cd /app/repo
    git pull
fi

# Ensure local pip bin is in PATH
export PATH="$HOME/.local/bin:$PATH"

# Run repo's start script
cd /app/repo
chmod +x start.sh
exec ./start.sh