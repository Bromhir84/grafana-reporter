FROM python:3.12-slim

# Install system dependencies for Python packages and Playwright
RUN apt-get update && apt-get install -y \
    git \
    libjpeg-dev \
    zlib1g-dev \
    gcc \
    curl \
    wget \
    gnupg \
    ca-certificates \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libasound2 \
    fonts-liberation \
    libwoff1 \
    libxfixes3 \
    && rm -rf /var/lib/apt/lists/*

# Create app user and directories
RUN useradd -m appuser && mkdir -p /app/repo

WORKDIR /app

# Copy setup script
COPY setup.sh /app/setup.sh

# Copy your repository (if needed)
# COPY . /app/repo

# Change ownership and permissions
RUN chown -R appuser:appuser /app && chmod +x /app/setup.sh

# Switch to non-root user
USER appuser

# Ensure local pip bin is in PATH
ENV PATH=/home/appuser/.local/bin:$PATH

# Install Python dependencies
RUN pip install --user --no-cache-dir playwright

# Install Playwright browsers at build time
RUN playwright install

# Entrypoint to start your app
ENTRYPOINT ["/app/setup.sh"]