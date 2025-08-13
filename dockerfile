FROM python:3.12-slim

RUN apt-get update && apt-get install -y git-all libjpeg-dev zlib1g-dev gcc && rm -rf /var/lib/apt/lists/*
# Set working directory
WORKDIR /app
RUN useradd -m appuser && mkdir -p /app/repo

# Copy the setup script.
COPY setup.sh /app/setup.sh


# Change ownership of the app directory to the new user
RUN chown -R appuser:appuser /app && chmod +x /app/setup.sh


USER appuser

# Set the script as executable


# Entrypoint to start your app
ENTRYPOINT ["/app/setup.sh"]