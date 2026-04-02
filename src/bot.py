"""
Telegram Bot Handlers — python-telegram-bot v21+

Receives user messages, validates URLs, enqueues download jobs,
and reports queue status.

Commands:
    /start, /help  → Welcome message with usage instructions
    /status        → Show queue length and active jobs
    /cancel        → Cancel current user's pending job
    <any URL>      → Validate URL, enqueue download job
"""
import re
import logging

from redis import Redis
from rq import Queue, Retry
from rq.job import Job
from rq.registry import StartedJobRegistry

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from src import config

logger = logging.getLogger(__name__)

# ── Redis & Queue ────────────────────────────────────────────────────────────

redis_conn = Redis.from_url(config.REDIS_URL)
queue = Queue("video-downloads", connection=redis_conn)

# ── Helpers ──────────────────────────────────────────────────────────────────

URL_REGEX = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

# Redis key prefix for tracking user jobs
USER_JOBS_KEY = "user_jobs:{user_id}"


def _is_allowed(user_id: int) -> bool:
    """Check if a user is allowed to use the bot."""
    if not config.ALLOWED_USERS:
        return True  # No restriction
    return user_id in config.ALLOWED_USERS


def _get_user_active_jobs(user_id: int) -> list[str]:
    """Get list of active/queued job IDs for a user."""
    key = USER_JOBS_KEY.format(user_id=user_id)
    job_ids = redis_conn.lrange(key, 0, -1)
    active = []
    for jid in job_ids:
        jid_str = jid.decode() if isinstance(jid, bytes) else jid
        try:
            job = Job.fetch(jid_str, connection=redis_conn)
            if job.get_status() in ("queued", "started", "deferred", "scheduled"):
                active.append(jid_str)
        except Exception:
            pass  # Job no longer exists
    # Update the list to only active jobs
    redis_conn.delete(key)
    for jid in active:
        redis_conn.rpush(key, jid)
    redis_conn.expire(key, 7200)  # TTL: 2 hours
    return active


def _track_user_job(user_id: int, job_id: str):
    """Track a job ID for a user."""
    key = USER_JOBS_KEY.format(user_id=user_id)
    redis_conn.rpush(key, job_id)
    redis_conn.expire(key, 7200)


def _validate_url(url: str) -> bool:
    """Validate that a URL is safe to process."""
    if not url.startswith(("http://", "https://")):
        return False
    # Block dangerous schemes that might be embedded
    dangerous = ("file://", "ftp://", "javascript:", "data:")
    if any(url.lower().startswith(d) for d in dangerous):
        return False
    return True


# ── Command Handlers ─────────────────────────────────────────────────────────

WELCOME_TEXT = """
🎬 *Video Downloader Bot*

Send me a URL and I'll download the video for you\\!

*Supported:*
• Direct video links \\(mp4, webm, etc\\.\\)
• HLS streams \\(m3u8, including encrypted\\)
• Cloudflare\\-protected pages

*Commands:*
/start \\- Show this message
/help \\- Show this message
/status \\- Check queue status
/cancel \\- Cancel your pending download

Just paste a URL to get started\\! 🚀
"""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start and /help commands."""
    await update.message.reply_text(
        WELCOME_TEXT,
        parse_mode="MarkdownV2",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command — show queue info."""
    user_id = update.effective_user.id

    if not _is_allowed(user_id):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    queued_count = queue.count
    started_registry = StartedJobRegistry(queue=queue)
    active_count = len(started_registry)
    user_jobs = _get_user_active_jobs(user_id)

    status_text = (
        f"📊 *Queue Status*\n\n"
        f"📋 Queued: {queued_count}\n"
        f"⚙️ Active: {active_count}\n"
        f"👤 Your jobs: {len(user_jobs)}\n"
    )

    if user_jobs:
        for jid in user_jobs:
            try:
                job = Job.fetch(jid, connection=redis_conn)
                status_text += f"\n  • `{jid[:8]}` — {job.get_status()}"
            except Exception:
                pass

    await update.message.reply_text(status_text, parse_mode="Markdown")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cancel command — cancel user's pending jobs."""
    user_id = update.effective_user.id

    if not _is_allowed(user_id):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    user_jobs = _get_user_active_jobs(user_id)

    if not user_jobs:
        await update.message.reply_text("🤷 You have no active or queued downloads.")
        return

    cancelled = 0
    for jid in user_jobs:
        try:
            job = Job.fetch(jid, connection=redis_conn)
            if job.get_status() in ("queued", "deferred", "scheduled"):
                job.cancel()
                cancelled += 1
            elif job.get_status() == "started":
                # Can't easily cancel a running job, but we can try
                job.cancel()
                cancelled += 1
        except Exception as e:
            logger.warning(f"Failed to cancel job {jid}: {e}")

    # Clear the user's job list
    key = USER_JOBS_KEY.format(user_id=user_id)
    redis_conn.delete(key)

    await update.message.reply_text(
        f"🗑️ Cancelled {cancelled} job(s)."
    )


# ── URL Handler ──────────────────────────────────────────────────────────────

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle messages containing URLs — enqueue download jobs."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    message_text = update.message.text or ""

    # Access control
    if not _is_allowed(user_id):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    # Extract URL
    match = URL_REGEX.search(message_text)
    if not match:
        return  # No URL found, ignore

    url = match.group(0)

    # Validate URL
    if not _validate_url(url):
        await update.message.reply_text(
            "⚠️ Invalid or unsupported URL. Please send an HTTP(S) link."
        )
        return

    # Rate limiting: check active jobs
    user_jobs = _get_user_active_jobs(user_id)
    if len(user_jobs) >= config.MAX_JOBS_PER_USER:
        await update.message.reply_text(
            f"⚠️ You already have {len(user_jobs)} active download(s). "
            f"Maximum is {config.MAX_JOBS_PER_USER}.\n"
            f"Use /cancel to cancel pending jobs or wait for them to finish."
        )
        return

    # Send initial status message
    status_msg = await update.message.reply_text(
        f"⏳ Queued for download (position #{queue.count + 1})\n"
        f"🔗 {url[:80]}{'...' if len(url) > 80 else ''}"
    )

    # Enqueue the job
    try:
        job = queue.enqueue(
            "src.tasks.download_and_upload",
            args=(url, chat_id, status_msg.message_id),
            job_timeout=config.DOWNLOAD_TIMEOUT,
            result_ttl=300,  # Keep result for 5 minutes
            retry=Retry(max=2, interval=60),
        )

        _track_user_job(user_id, job.id)

        logger.info(
            f"Job {job.id[:8]} enqueued for user {user_id}: {url[:80]}"
        )

        # Update message with job ID
        await status_msg.edit_text(
            f"⏳ Queued for download (position #{queue.count})\n"
            f"🔗 {url[:80]}{'...' if len(url) > 80 else ''}\n"
            f"🆔 Job: `{job.id[:8]}`"
        , parse_mode="Markdown")

    except Exception as e:
        logger.exception(f"Failed to enqueue job for {url}")
        await status_msg.edit_text(
            f"❌ Failed to queue download: {str(e)[:100]}"
        )


# ── Error Handler ────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log errors and notify the user if possible."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ An unexpected error occurred. Please try again."
            )
        except Exception:
            pass


# ── Application Entry Point ──────────────────────────────────────────────────

def main():
    """Build and run the Telegram bot application."""
    logger.info("Starting Telegram Video Downloader Bot...")
    logger.info(f"Bot API URL: {config.LOCAL_API_BASE_URL}")
    logger.info(f"Redis URL: {config.REDIS_URL}")
    logger.info(f"Download dir: {config.DOWNLOAD_DIR}")
    logger.info(f"Max file size: {config.MAX_FILE_SIZE_MB} MB")

    if config.ALLOWED_USERS:
        logger.info(f"Access restricted to users: {config.ALLOWED_USERS}")
    else:
        logger.info("Access: PUBLIC (no user restrictions)")

    # Build the application with local Bot API server
    app = (
        ApplicationBuilder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .base_url(config.LOCAL_API_BASE_URL)
        .base_file_url(config.LOCAL_API_BASE_FILE_URL)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(30)
        .build()
    )

    # Register handlers
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # URL handler: catch any message with an HTTP(S) URL
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(URL_REGEX),
        handle_url,
    ))

    # Error handler
    app.add_error_handler(error_handler)

    # Start polling
    logger.info("Bot is running. Polling for updates...")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message"],
    )


if __name__ == "__main__":
    main()
