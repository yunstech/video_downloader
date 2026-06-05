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
import os
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
from src.terabox import TERABOX_DOMAINS
from src.live_recorder import RECORD_STOP_KEY

logger = logging.getLogger(__name__)

# ── Redis & Queues ───────────────────────────────────────────────────────────

redis_conn = Redis.from_url(config.REDIS_URL)
queue = Queue("video-downloads", connection=redis_conn)
terabox_queue = Queue("terabox-downloads", connection=redis_conn)
# Dedicated queue for live recordings — workers use job_timeout=-1
live_queue = Queue("live-recordings", connection=redis_conn)

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


def _is_terabox_url(url: str) -> bool:
    """Check if a URL is a Terabox link."""
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.lower().replace("www.", "")
        return any(d in domain for d in TERABOX_DOMAINS)
    except Exception:
        return False


# ── Command Handlers ─────────────────────────────────────────────────────────

WELCOME_TEXT = """
🎬 *Video Downloader Bot*

Send me a URL and I'll download the video for you\\!

*Supported:*
• Direct video links \\(mp4, webm, etc\\.\\)
• HLS streams \\(m3u8, including encrypted\\)
• Cloudflare\\-protected pages
• YouTube live / membership streams

*Commands:*
/start \- Show this message
/help \- Show this message
/status \- Check queue status
/cancel \- Cancel your pending download
/record \<URL\> \- Record a YouTube live stream \(480p\)
/record\_start \<URL\> \- Same but record from the beginning \(DVR\)
/stoprecord \- Stop your active live recording early
/setcookies \- Upload cookies\.txt for membership streams

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

    tb_queued = terabox_queue.count
    tb_started_registry = StartedJobRegistry(queue=terabox_queue)
    tb_active = len(tb_started_registry)

    user_jobs = _get_user_active_jobs(user_id)

    status_text = (
        f"📊 *Queue Status*\n\n"
        f"📋 Downloads: {queued_count} queued, {active_count} active\n"
        f"📦 Terabox: {tb_queued} queued, {tb_active} active\n"
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
    is_terabox = _is_terabox_url(url)
    target_queue = terabox_queue if is_terabox else queue
    queue_label = "Terabox" if is_terabox else "download"

    status_msg = await update.message.reply_text(
        f"⏳ Queued for {queue_label} (position #{target_queue.count + 1})\n"
        f"🔗 {url[:80]}{'...' if len(url) > 80 else ''}"
    )

    # Enqueue the job
    try:
        job = target_queue.enqueue(
            "src.tasks.download_and_upload",
            args=(url, chat_id, status_msg.message_id),
            job_timeout=config.DOWNLOAD_TIMEOUT,
            result_ttl=300,  # Keep result for 5 minutes
            retry=Retry(max=2, interval=60),
        )

        _track_user_job(user_id, job.id)

        logger.info(
            f"Job {job.id[:8]} enqueued on '{target_queue.name}' for user {user_id}: {url[:80]}"
        )

        # Update message with job ID
        await status_msg.edit_text(
            f"⏳ Queued for {queue_label} (position #{target_queue.count})\n"
            f"🔗 {url[:80]}{'...' if len(url) > 80 else ''}\n"
            f"🆔 Job: {job.id[:8]}"
        )

    except Exception as e:
        logger.exception(f"Failed to enqueue job for {url}")
        await status_msg.edit_text(
            f"❌ Failed to queue download: {str(e)[:100]}"
        )


# ── Error Handler ────────────────────────────────────────────────────────────

# ── Live Recorder Helpers ───────────────────────────────────────────────────

USER_LIVE_JOB_KEY = "user_live_job:{user_id}"


def _cookies_path(user_id: int) -> str:
    """Return the path where a user's cookies.txt is stored."""
    return os.path.join(config.COOKIES_DIR, f"{user_id}.txt")


def _is_youtube_url(url: str) -> bool:
    """Return True for youtube.com / youtu.be URLs."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower().replace("www.", "")
        return host in ("youtube.com", "youtu.be", "m.youtube.com")
    except Exception:
        return False


# ── /record and /stoprecord Handlers ────────────────────────────────────────

async def cmd_record(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    live_from_start: bool = False,
):
    """Handle /record <URL> — enqueue a live recording job."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if not _is_allowed(user_id):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /record <YouTube live URL>\n"
            "Example: /record https://youtube.com/watch?v=XXXX\n\n"
            "For membership streams, upload your cookies.txt first with /setcookies."
        )
        return

    url = args[0].strip()
    if not _validate_url(url):
        await update.message.reply_text("⚠️ Invalid URL. Please send an HTTP(S) link.")
        return

    if not _is_youtube_url(url):
        await update.message.reply_text(
            "⚠️ /record only supports YouTube URLs.\n"
            "For other sites just paste the URL directly."
        )
        return

    # Check for existing live recording job
    live_job_key = USER_LIVE_JOB_KEY.format(user_id=user_id)
    existing_jid = redis_conn.get(live_job_key)
    if existing_jid:
        jid_str = existing_jid.decode() if isinstance(existing_jid, bytes) else existing_jid
        try:
            job = Job.fetch(jid_str, connection=redis_conn)
            if job.get_status() in ("queued", "started"):
                await update.message.reply_text(
                    f"⚠️ You already have an active recording (job `{jid_str[:8]}`).\n"
                    "Use /stoprecord to stop it first.",
                    parse_mode="Markdown",
                )
                return
        except Exception:
            pass  # Job gone, allow new one

    cookies_path = _cookies_path(user_id)
    has_cookies = os.path.isfile(cookies_path)

    status_msg = await update.message.reply_text(
        f"⏳ Queuing live recording…\n"
        f"🔗 {url[:80]}{'...' if len(url) > 80 else ''}\n"
        f"🍪 Cookies: {'✅ found' if has_cookies else '❌ none (use /setcookies for memberships)'}"
    )

    try:
        job = live_queue.enqueue(
            "src.tasks.record_live",
            kwargs={
                "url": url,
                "chat_id": chat_id,
                "status_message_id": status_msg.message_id,
                "user_id": user_id,
                "cookies_path": cookies_path if has_cookies else None,
                "live_from_start": live_from_start,
            },
            job_timeout=-1,   # No timeout — recording can take 10+ hours
            result_ttl=600,
        )

        # Store job ID so /stoprecord can find it
        redis_conn.set(live_job_key, job.id, ex=86400)
        _track_user_job(user_id, job.id)

        logger.info(f"Live recording job {job.id[:8]} queued for user {user_id}: {url[:80]}")

        await status_msg.edit_text(
            f"⏺️ Recording queued (job `{job.id[:8]}`)\n"
            f"🔗 {url[:80]}{'...' if len(url) > 80 else ''}\n"
            f"🍪 Cookies: {'✅' if has_cookies else '❌ none'}\n"
            f"📐 Quality: 480p · H.264 CRF-28 · AAC 64 kbps\n"
            f"🛑 Send /stoprecord to stop early.",
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.exception(f"Failed to queue live recording for {url}")
        await status_msg.edit_text(f"❌ Failed to queue recording: {str(e)[:100]}")


async def cmd_record_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /record_start <URL> — record from beginning (DVR)."""
    await cmd_record(update, context, live_from_start=True)


async def cmd_stoprecord(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stoprecord — set Redis stop flag to gracefully end recording."""
    user_id = update.effective_user.id

    if not _is_allowed(user_id):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    live_job_key = USER_LIVE_JOB_KEY.format(user_id=user_id)
    existing_jid = redis_conn.get(live_job_key)

    if not existing_jid:
        await update.message.reply_text("🤷 No active live recording found.")
        return

    # Set the stop flag — live_recorder polls this and terminates yt-dlp
    stop_key = RECORD_STOP_KEY.format(user_id=user_id)
    redis_conn.set(stop_key, "1", ex=300)  # 5-minute TTL as safety
    redis_conn.delete(live_job_key)

    await update.message.reply_text(
        "🛑 Stop signal sent.\n"
        "The recording will finish the current segment, then upload the file."
    )


async def cmd_setcookies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setcookies — prompt the user to upload cookies.txt."""
    user_id = update.effective_user.id

    if not _is_allowed(user_id):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    cookies_path = _cookies_path(user_id)
    if os.path.isfile(cookies_path):
        size = os.path.getsize(cookies_path)
        await update.message.reply_text(
            f"🍪 You already have cookies stored ({size} bytes).\n"
            "Send a new cookies.txt file to replace them, "
            "or use /record directly."
        )
    else:
        await update.message.reply_text(
            "🍪 *How to set cookies for membership streams:*\n\n"
            "1\. Install the *Get cookies\.txt LOCALLY* browser extension\n"
            "2\. Visit youtube\.com while logged in\n"
            "3\. Export cookies as `cookies\.txt` \(Netscape format\)\n"
            "4\. Send the file here as a document\n\n"
            "_Your cookies are stored only on this server\._",
            parse_mode="MarkdownV2",
        )


async def handle_cookies_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle document uploads — save cookies.txt files for membership recording.
    The file must be named 'cookies.txt' or the user's caption must contain 'cookies'.
    """
    user_id = update.effective_user.id

    if not _is_allowed(user_id):
        return

    doc = update.message.document
    if not doc:
        return

    filename = (doc.file_name or "").lower()
    caption = (update.message.caption or "").lower()

    # Only accept files named cookies.txt or sent with a cookies caption
    if "cookies" not in filename and "cookies" not in caption:
        return

    # Reject suspiciously large files (> 2 MB)
    if doc.file_size and doc.file_size > 2 * 1024 * 1024:
        await update.message.reply_text(
            "⚠️ File too large. A cookies.txt should be well under 2 MB."
        )
        return

    await update.message.reply_text("⬇️ Saving cookies…")

    try:
        os.makedirs(config.COOKIES_DIR, exist_ok=True)
        cookies_path = _cookies_path(user_id)

        tg_file = await doc.get_file()
        await tg_file.download_to_drive(cookies_path)

        # Basic sanity check — Netscape cookies start with a comment line
        with open(cookies_path, "r", encoding="utf-8", errors="ignore") as f:
            first_line = f.readline()
        if "netscape" not in first_line.lower() and not first_line.startswith("#"):
            os.remove(cookies_path)
            await update.message.reply_text(
                "⚠️ File doesn't look like a Netscape cookies.txt.\n"
                "Export from your browser using the \'Get cookies.txt LOCALLY\' extension."
            )
            return

        size = os.path.getsize(cookies_path)
        await update.message.reply_text(
            f"✅ Cookies saved ({size} bytes).\n"
            "Use /record <URL> to start recording a membership stream."
        )
        logger.info(f"Cookies saved for user {user_id} ({size} bytes)")

    except Exception as e:
        logger.exception(f"Failed to save cookies for user {user_id}")
        await update.message.reply_text(f"❌ Failed to save cookies: {str(e)[:100]}")


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
    app.add_handler(CommandHandler("record", cmd_record))
    app.add_handler(CommandHandler("record_start", cmd_record_start))
    app.add_handler(CommandHandler("stoprecord", cmd_stoprecord))
    app.add_handler(CommandHandler("setcookies", cmd_setcookies))

    # Document handler for cookies.txt uploads
    app.add_handler(MessageHandler(filters.Document.ALL, handle_cookies_upload))

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
