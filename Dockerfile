FROM python:3.11

# Install system dependencies: ffmpeg, curl, and libraries for Playwright/Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    # Chromium runtime dependencies
    libglib2.0-0 \
    libglib2.0-bin \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgdk-pixbuf2.0-0 \
    libgtk-3-0 \
    libgtk-3-common \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libxss1 \
    xdg-utils \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# Upgrade pip and install build tools for compiling packages like curl_cffi
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (chromium)
RUN playwright install chromium

COPY src/ src/

RUN mkdir -p /app/downloads

ENV PYTHONUNBUFFERED=1

# Default: run bot (override in compose for worker)
CMD ["python", "-m", "src.bot"]
