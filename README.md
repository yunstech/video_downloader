# Telegram Video Downloader Bot 🎬

A Telegram bot that accepts a URL, extracts and downloads videos (including Cloudflare-protected and AES-128 encrypted HLS streams), and uploads the result back to the user.

## Features

- 🌐 **Headless Browser** — Uses Playwright to render JavaScript-heavy pages (Twitter/X, Vimeo, etc.)
- 🛡️ **Cloudflare Bypass** — Falls back to `curl_cffi` with browser TLS impersonation
- 🔐 **HLS Decryption** — Supports AES-128 encrypted `.m3u8` streams
- 📦 **Large File Uploads** — Self-hosted Telegram Bot API supports up to 2 GB
- ⚡ **Async Queue** — Redis-backed task queue (RQ) for concurrent downloads
- 🐳 **Docker Compose** — One command to run everything
- 🔒 **Access Control** — Optional user whitelist

## Architecture

```
Telegram Cloud ↔ Local Bot API Server (:8081) ↔ Bot App (Python)
                                                    │
                                              enqueue to Redis
                                                    │
                                              RQ Worker(s)
                                              └── video_downloader
```

## Quick Start

### Prerequisites

- Docker & Docker Compose
- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- Telegram API ID & Hash (from [my.telegram.org](https://my.telegram.org))

### Setup

```bash
# 1. Clone the repository
git clone <your-repo-url>
cd telegram-video-bot

# 2. Configure environment
cp .env.example .env
# Edit .env with your credentials:
#   TELEGRAM_BOT_TOKEN=your-bot-token
#   TELEGRAM_API_ID=your-api-id
#   TELEGRAM_API_HASH=your-api-hash

# 3. Build and start
docker compose up -d --build

# 4. Check logs
docker compose logs -f bot
docker compose logs -f worker

# 5. Scale workers for concurrent downloads
docker compose up -d --scale worker=3
```

### Stop

```bash
# Stop all services
docker compose down

# Full reset (removes volumes)
docker compose down -v
```

## Usage

1. Start a chat with your bot on Telegram
2. Send `/start` for usage instructions
3. Paste any URL containing a video
4. The bot downloads the video and sends it back!

### Bot Commands

| Command   | Description                     |
|-----------|---------------------------------|
| `/start`  | Show welcome message and usage  |
| `/help`   | Show usage instructions         |
| `/status` | Check queue status              |
| `/cancel` | Cancel your pending download    |

## Environment Variables

| Variable                  | Default                        | Description                              |
|---------------------------|--------------------------------|------------------------------------------|
| `TELEGRAM_BOT_TOKEN`     | *(required)*                   | Bot token from @BotFather                |
| `TELEGRAM_API_ID`        | *(required)*                   | API ID from my.telegram.org              |
| `TELEGRAM_API_HASH`      | *(required)*                   | API Hash from my.telegram.org            |
| `TELEGRAM_BOT_API_URL`   | `http://telegram-bot-api:8081` | Local Bot API server URL                 |
| `REDIS_URL`              | `redis://redis:6379/0`         | Redis connection URL                     |
| `DOWNLOAD_DIR`           | `/app/downloads`               | Temp directory for downloads             |
| `MAX_FILE_SIZE_MB`       | `2000`                         | Max file size to upload (MB)             |
| `MAX_CONCURRENT_DOWNLOADS` | `3`                          | Max simultaneous workers                 |
| `DOWNLOAD_TIMEOUT`       | `1800`                         | Download timeout in seconds (30 min)     |
| `ALLOWED_USERS`          | *(empty = public)*             | Comma-separated Telegram user IDs        |
| `LOG_LEVEL`              | `INFO`                         | Logging level                            |

## Project Structure

```
├── docker-compose.yml          # Service orchestration
├── Dockerfile                  # Python app container
├── requirements.txt            # Python dependencies
├── .env.example                # Environment template
├── src/
│   ├── __init__.py
│   ├── bot.py                  # Telegram bot handlers
│   ├── worker.py               # RQ worker entry point
│   ├── tasks.py                # Queue task definitions
│   ├── downloader.py           # Adapter for video_downloader
│   ├── config.py               # Settings from env vars
│   └── video_downloader.py     # Core download engine
├── scripts/
│   └── entrypoint.sh           # Container entrypoint
└── downloads/                  # Temp files (Docker volume)
```

## How It Works

1. **User sends a URL** → Bot validates and enqueues a download job to Redis
2. **RQ Worker picks up the job** → Calls `video_downloader.py` to:
   - Fetch the page (bypassing Cloudflare with `curl_cffi`)
   - Extract video URLs from HTML/JS
   - Download the video (direct or HLS with decryption)
3. **Worker uploads the video** → Uses local Bot API (supports up to 2 GB)
4. **Cleanup** → Temp files are deleted after upload

## Self-Hosted Telegram Bot API

The self-hosted [Telegram Bot API server](https://github.com/tdlib/telegram-bot-api) is required to upload files larger than 50 MB (up to 2 GB).

### Why?
- The public `api.telegram.org` limits uploads to **50 MB**
- The local server removes this limit (up to **2000 MB**)
- Files are transferred via shared Docker volume (no re-upload overhead)

### Getting API Credentials
1. Go to [my.telegram.org](https://my.telegram.org)
2. Log in with your phone number
3. Go to "API development tools"
4. Create an application to get `API_ID` and `API_HASH`

> **Note:** These are NOT the bot token. The bot token comes from @BotFather.

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Bot not responding | Check logs: `docker compose logs -f bot` |
| "Unauthorized" error | Verify `TELEGRAM_BOT_TOKEN` is correct |
| Upload fails for large files | Ensure local Bot API is running: `docker compose logs telegram-bot-api` |
| Bot API crashes on start | Verify `API_ID` and `API_HASH` from my.telegram.org |
| Worker not processing jobs | Check queue name matches and Redis is running |
| Download fails | Check worker logs: `docker compose logs -f worker` |

## Security Notes

- Set `ALLOWED_USERS` in production to restrict access
- Don't expose port 8081 publicly (remove `ports` mapping or bind to 127.0.0.1)
- URL validation rejects `file://`, `ftp://`, `javascript:` schemes
- Each user is rate-limited to 3 concurrent jobs by default

## License

MIT
