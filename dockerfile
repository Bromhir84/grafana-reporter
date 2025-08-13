FROM python:3.12-slim

RUN apt-get update
RUN apt-get install git-all -y

# Set working directory
WORKDIR /app

# Copy the setup script.
COPY setup.sh /app/setup.sh

# Set the script as executable
RUN chmod +x /app/setup.sh

# Entrypoint to start your app
ENTRYPOINT ["/app/setup.sh", ""]