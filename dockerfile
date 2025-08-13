FROM python:3.12-slim
RUN apt-get update && apt-get install -y \
    libjpeg-dev zlib1g-dev gcc \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
COPY generate_report.py .
COPY report_api.py .
RUN pip install --no-cache-dir -r requirements.txt

# Entrypoint
ENTRYPOINT ["python", "/app/generate_report.py"]