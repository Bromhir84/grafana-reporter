#!/bin/bash

# Install required system packages
apt-get update && apt-get install -y \
    libjpeg-dev zlib1g-dev gcc git \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
pip install --no-cache-dir -r requirements.txt

./app/report_api.py