#!/bin/bash
set -e

echo "==================================="
echo "  Telegram Video Downloader Bot"
echo "==================================="

# Ensure downloads directory exists
mkdir -p "${DOWNLOAD_DIR:-/app/downloads}"

# Wait for Redis to be ready
echo "Waiting for Redis..."
MAX_RETRIES=30
RETRY_COUNT=0
while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if redis-cli -u "${REDIS_URL:-redis://redis:6379/0}" ping > /dev/null 2>&1; then
        echo "Redis is ready!"
        break
    fi
    RETRY_COUNT=$((RETRY_COUNT + 1))
    echo "  Waiting for Redis... ($RETRY_COUNT/$MAX_RETRIES)"
    sleep 2
done

if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
    echo "ERROR: Redis is not available after $MAX_RETRIES attempts"
    exit 1
fi

# Run the specified command or default to bot
if [ "$1" = "worker" ]; then
    echo "Starting RQ worker..."
    exec python -m src.worker
elif [ "$1" = "bot" ]; then
    echo "Starting Telegram bot..."
    exec python -m src.bot
else
    # Default: run whatever was passed
    exec "$@"
fi
