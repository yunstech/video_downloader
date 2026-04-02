"""
RQ task definitions for the video download queue.

These functions are executed by rq workers. Each task:
1. Downloads a video using the downloader adapter
2. Uploads the result to Telegram via the local Bot API
3. Cleans up temporary files
4. Reports progress back to the user via message edits
"""
import os
import time
import logging
import requests

from src import config
from src.downloader import download_video

logger = logging.getLogger(__name__)

# ── Telegram API Helpers ─────────────────────────────────────────────────────

API_BASE = f"{config.TELEGRAM_BOT_API_URL}/bot{config.TELEGRAM_BOT_TOKEN}"


def _edit_message(chat_id: int, message_id: int, text: str):
    """Edit a Telegram message with the given text."""
    try:
        resp = requests.post(
            f"{API_BASE}/editMessageText",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        if not resp.ok:
            logger.warning(f"Failed to edit message: {resp.text}")
    except Exception as e:
        logger.warning(f"Failed to edit message: {e}")


def _send_video(chat_id: int, filepath: str, caption: str = None):
    """
    Upload a video to Telegram via the local Bot API.

    Uses file:// URI for shared-volume mode (fastest),
    falls back to multipart upload.
    """
    filename = os.path.basename(filepath)
    file_size = os.path.getsize(filepath)

    # Strategy 1: file:// URI (shared volume, no re-upload)
    try:
        logger.info(f"Attempting file:// upload for {filename} ({file_size} bytes)")
        resp = requests.post(
            f"{API_BASE}/sendVideo",
            json={
                "chat_id": chat_id,
                "video": f"file://{filepath}",
                "caption": caption or "",
                "supports_streaming": True,
            },
            timeout=config.DOWNLOAD_TIMEOUT,
        )
        if resp.ok:
            logger.info("file:// upload succeeded")
            return resp.json()
        else:
            logger.warning(f"file:// upload failed ({resp.status_code}): {resp.text}")
    except Exception as e:
        logger.warning(f"file:// upload failed: {e}")

    # Strategy 2: Multipart upload
    logger.info(f"Falling back to multipart upload for {filename}")
    try:
        with open(filepath, "rb") as f:
            resp = requests.post(
                f"{API_BASE}/sendVideo",
                data={
                    "chat_id": chat_id,
                    "supports_streaming": "true",
                    "caption": caption or "",
                },
                files={"video": (filename, f, "video/mp4")},
                timeout=config.DOWNLOAD_TIMEOUT,
            )
        if resp.ok:
            logger.info("Multipart upload succeeded")
            return resp.json()
        else:
            logger.warning(f"Multipart upload failed ({resp.status_code}): {resp.text}")
    except Exception as e:
        logger.warning(f"Multipart upload failed: {e}")

    # Strategy 3: Send as document (last resort for non-video files)
    logger.info(f"Attempting sendDocument fallback for {filename}")
    try:
        with open(filepath, "rb") as f:
            resp = requests.post(
                f"{API_BASE}/sendDocument",
                data={
                    "chat_id": chat_id,
                    "caption": caption or "",
                },
                files={"document": (filename, f, "application/octet-stream")},
                timeout=config.DOWNLOAD_TIMEOUT,
            )
        if resp.ok:
            logger.info("Document upload succeeded")
            return resp.json()
        else:
            raise RuntimeError(f"All upload strategies failed. Last: {resp.text}")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Upload failed: {e}")


# ── Main Task ────────────────────────────────────────────────────────────────

def download_and_upload(url: str, chat_id: int, status_message_id: int) -> dict:
    """
    Main queue task: download a video and upload it to Telegram.

    Args:
        url: The page URL containing the video.
        chat_id: Telegram chat ID to send the result to.
        status_message_id: Message ID to edit with progress updates.

    Returns:
        dict with result metadata.
    """
    start_time = time.time()
    filepath = None

    try:
        # ── Download ─────────────────────────────────────────────────────
        def progress_callback(text: str):
            _edit_message(chat_id, status_message_id, text)

        _edit_message(chat_id, status_message_id, "⬇️ Downloading video...")

        result = download_video(
            url=url,
            download_dir=config.DOWNLOAD_DIR,
            progress_callback=progress_callback,
        )

        filepath = result["filepath"]
        size_mb = result["size_mb"]

        logger.info(f"Downloaded: {filepath} ({size_mb:.1f} MB)")

        # ── Check file size ──────────────────────────────────────────────
        if size_mb > config.MAX_FILE_SIZE_MB:
            _edit_message(
                chat_id,
                status_message_id,
                f"❌ File too large ({size_mb:.1f} MB).\n"
                f"Maximum supported size is {config.MAX_FILE_SIZE_MB} MB.",
            )
            return {
                "status": "error",
                "reason": "file_too_large",
                "size_mb": size_mb,
            }

        # ── Upload to Telegram ───────────────────────────────────────────
        _edit_message(
            chat_id,
            status_message_id,
            f"⬆️ Uploading to Telegram... ({size_mb:.1f} MB)",
        )

        caption = f"📹 {result['filename']}\n📦 {size_mb:.1f} MB"
        _send_video(chat_id, filepath, caption=caption)

        # ── Final status ─────────────────────────────────────────────────
        elapsed = time.time() - start_time
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        time_str = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"

        _edit_message(
            chat_id,
            status_message_id,
            f"✅ Complete! ({size_mb:.1f} MB, {time_str})",
        )

        return {
            "status": "success",
            "filename": result["filename"],
            "size_mb": size_mb,
            "elapsed_seconds": round(elapsed, 1),
        }

    except Exception as e:
        logger.exception(f"Task failed for URL: {url}")
        error_msg = str(e)
        if len(error_msg) > 200:
            error_msg = error_msg[:200] + "..."

        _edit_message(
            chat_id,
            status_message_id,
            f"❌ Download failed: {error_msg}\n\n"
            f"💡 Try sending a direct video URL.",
        )

        return {
            "status": "error",
            "reason": error_msg,
        }

    finally:
        # ── Cleanup ──────────────────────────────────────────────────────
        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
                logger.info(f"Cleaned up: {filepath}")
            except OSError as e:
                logger.warning(f"Cleanup failed for {filepath}: {e}")
