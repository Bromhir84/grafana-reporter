#!/bin/bash

if [ ! -d "/app" ]; then
    git clone https://github.com/Bromhir84/grafana-reporter.git /app
else
    cd /app
    git pull
    cd /app
fi

chmod -x /app/start.sh
./app/start.sh