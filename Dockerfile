FROM python:3.11-slim

# Install system deps: ffmpeg, curl, ca-certificates, and Chromium runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    libnspr4 \
    libnss3 \
    libgconf-2-4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libgdk-pixbuf2.0-0 \
    libgtk-3-0 \
    libgbm1 \
    libasound2 \
    libxss1 \
    libxkbcommon0 \
    fonts-liberation \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (chromium)
RUN playwright install chromium

COPY src/ src/

RUN mkdir -p /app/downloads

ENV PYTHONUNBUFFERED=1

# Default: run bot (override in compose for worker)
CMD ["python", "-m", "src.bot"]
