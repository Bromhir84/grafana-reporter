FROM python:3.12-slim

RUN apt-get update
RUN apt-get install git-all -y
RUN apt-get update && apt-get install -y libjpeg-dev zlib1g-dev gcc git && rm -rf /var/lib/apt/lists/*
# Set working directory
WORKDIR /app

# Copy the setup script.
COPY setup.sh /app/setup.sh
RUN useradd -m appuser
RUN mkdir -p /app/repo

# Change ownership of the app directory to the new user
RUN chown -R appuser:appuser /app
RUN chown appuser:appuser /app/setup.sh
USER appuser

# Set the script as executable
RUN chmod +x /app/setup.sh

# Entrypoint to start your app
ENTRYPOINT ["/app/setup.sh", ""]