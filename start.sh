#!/bin/bash



# Install Python dependencies
pip install --no-cache-dir -r requirements.txt

./app/repo/uvicorn report_api:app --host 0.0.0.0 --port 8000
