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
import subprocess
import requests

from src import config
from src.downloader import download_video
from src.converter import convert_ts_to_mp4

logger = logging.getLogger(__name__)

# ── Telegram API Helpers ─────────────────────────────────────────────────────

API_BASE = f"{config.TELEGRAM_BOT_API_URL}/bot{config.TELEGRAM_BOT_TOKEN}"


def _edit_message(chat_id: int, message_id: int, text: str, use_markdown: bool = False):
    """Edit a Telegram message with the given text."""
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }
    if use_markdown:
        payload["parse_mode"] = "Markdown"
    try:
        resp = requests.post(
            f"{API_BASE}/editMessageText",
            json=payload,
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
    extra_filepaths = []

    logger.info(f"[TASK START] url={url} chat_id={chat_id} status_msg={status_message_id}")

    try:
        # ── Download ─────────────────────────────────────────────────────
        def progress_callback(text: str):
            logger.debug(f"[PROGRESS] {text}")
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

        # ── Convert .ts → .mp4 if needed ─────────────────────────────────
        if filepath.lower().endswith(".ts"):
            mp4_path = os.path.splitext(filepath)[0] + ".mp4"

            # Try 1: Local ffmpeg (fastest, already in Docker image)
            _edit_message(chat_id, status_message_id, "🔄 Converting .ts → .mp4 (ffmpeg)...")
            try:
                cmd = [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
                    "-i", filepath,
                    "-c", "copy",
                    "-bsf:a", "aac_adtstoasc",
                    "-movflags", "+faststart",
                    mp4_path,
                ]
                logger.debug(f"ffmpeg cmd: {' '.join(cmd)}")
                result_proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                logger.debug(f"ffmpeg returncode={result_proc.returncode} stderr={result_proc.stderr[:300]!r}")
                if result_proc.returncode == 0 and os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0:
                    logger.info(f"ffmpeg converted: {filepath} → {mp4_path}")
                    try:
                        os.remove(filepath)
                    except OSError:
                        pass
                    filepath = mp4_path
                    size_mb = os.path.getsize(filepath) / (1024 * 1024)
                else:
                    logger.warning(f"ffmpeg failed: {result_proc.stderr[:200]}")
                    raise RuntimeError("ffmpeg conversion failed")
            except Exception as e:
                logger.warning(f"ffmpeg conversion failed: {e}")
                # Clean up failed mp4 if it exists
                if os.path.exists(mp4_path):
                    try:
                        os.remove(mp4_path)
                    except OSError:
                        pass

                # Try 2: ConvertHub API
                _edit_message(chat_id, status_message_id, "🔄 Converting .ts → .mp4 (API)...")
                api_result = convert_ts_to_mp4(
                    filepath,
                    progress_callback=progress_callback,
                )
                if api_result:
                    filepath = api_result
                    size_mb = os.path.getsize(filepath) / (1024 * 1024)
                    logger.info(f"API converted to: {filepath} ({size_mb:.1f} MB)")
                else:
                    logger.warning("All conversion methods failed, uploading .ts as-is")

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

        # ── Upload extra files (multi-file Terabox downloads) ────────────
        extra_files = result.get("extra_files", [])
        uploaded_count = 1  # already uploaded the first one
        extra_filepaths = []  # track for cleanup

        for extra in extra_files:
            extra_fp = extra.get("filepath", "")
            extra_filepaths.append(extra_fp)
            extra_size = extra.get("size_mb", 0)

            if extra_size > config.MAX_FILE_SIZE_MB:
                logger.warning(f"Extra file too large ({extra_size:.1f} MB): {extra_fp}")
                _edit_message(
                    chat_id,
                    status_message_id,
                    f"⚠️ Skipping {extra['filename']} — too large ({extra_size:.1f} MB)",
                )
                continue

            try:
                _edit_message(
                    chat_id,
                    status_message_id,
                    f"⬆️ Uploading [{uploaded_count + 1}/{1 + len(extra_files)}] "
                    f"({extra_size:.1f} MB)...",
                )
                extra_caption = f"📹 {extra['filename']}\n📦 {extra_size:.1f} MB"
                _send_video(chat_id, extra_fp, caption=extra_caption)
                uploaded_count += 1
            except Exception as e:
                logger.warning(f"Failed to upload extra file {extra_fp}: {e}")

        # ── Final status ─────────────────────────────────────────────────
        total_uploaded_mb = size_mb + sum(
            ef.get("size_mb", 0) for ef in extra_files
        )
        elapsed = time.time() - start_time
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        time_str = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"

        if uploaded_count > 1:
            _edit_message(
                chat_id,
                status_message_id,
                f"✅ Complete! {uploaded_count} files ({total_uploaded_mb:.1f} MB, {time_str})",
            )
        else:
            _edit_message(
                chat_id,
                status_message_id,
                f"✅ Complete! ({size_mb:.1f} MB, {time_str})",
            )

        return {
            "status": "success",
            "filename": result["filename"],
            "size_mb": size_mb,
            "files_uploaded": uploaded_count,
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

        # Clean up extra files from multi-file downloads
        for efp in extra_filepaths:
            if efp and os.path.exists(efp):
                try:
                    os.remove(efp)
                    logger.info(f"Cleaned up extra: {efp}")
                except OSError as e:
                    logger.warning(f"Cleanup failed for extra {efp}: {e}")
