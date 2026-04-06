FROM python:3.11

# Install system dependencies: ffmpeg, curl, aria2
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    aria2 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# Upgrade pip and install Python packages
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser AND its system dependencies
RUN playwright install --with-deps chromium

COPY src/ src/

RUN mkdir -p /app/downloads

ENV PYTHONUNBUFFERED=1

# Default: run bot (override in compose for worker)
CMD ["python", "-m", "src.bot"]
