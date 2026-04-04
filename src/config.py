"""
Centralized settings loaded from environment variables.
Provides defaults where sensible. Validates on import.
"""
import os
import logging

# ── Telegram ─────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# Point python-telegram-bot at the local API server
TELEGRAM_BOT_API_URL = os.getenv(
    "TELEGRAM_BOT_API_URL", "http://telegram-bot-api:8081"
)

# python-telegram-bot's base_url format: http://host:port/bot
LOCAL_API_BASE_URL = f"{TELEGRAM_BOT_API_URL}/bot"

# For getFile to work in local mode:
LOCAL_API_BASE_FILE_URL = f"{TELEGRAM_BOT_API_URL}/file/bot"

# ── Redis ────────────────────────────────────────────────────────────────────

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# ── Downloads ────────────────────────────────────────────────────────────────

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/app/downloads")
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "2000"))
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "1800"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))

# ── Access Control ───────────────────────────────────────────────────────────

_allowed = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS: set[int] = (
    {int(uid.strip()) for uid in _allowed.split(",") if uid.strip()}
    if _allowed else set()
)

# ── Logging ──────────────────────────────────────────────────────────────────

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ── Rate Limiting ────────────────────────────────────────────────────────────

MAX_JOBS_PER_USER = int(os.getenv("MAX_JOBS_PER_USER", "3"))

# ── ConvertHub API (ts → mp4 conversion) ────────────────────────────────────

CONVERTHUB_API_KEY = os.getenv("CONVERTHUB_API_KEY", "")
