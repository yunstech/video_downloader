"""
ConvertHub API client for .ts → .mp4 conversion.

Used as a fallback when ffmpeg is not available and the downloaded
video is in .ts format.

API docs: https://api.converthub.com
"""
import os
import time
import logging
import requests

from src import config

logger = logging.getLogger(__name__)

CONVERTHUB_BASE_URL = "https://api.converthub.com/v2"


def convert_ts_to_mp4(
    ts_filepath: str,
    progress_callback=None,
    timeout: int = 600,
    poll_interval: int = 5,
) -> str | None:
    """
    Convert a .ts file to .mp4 using the ConvertHub API.

    Args:
        ts_filepath: Absolute path to the .ts file.
        progress_callback: Optional callable(status_text) for updates.
        timeout: Max wait time in seconds for conversion.
        poll_interval: Seconds between status checks.

    Returns:
        Path to the converted .mp4 file, or None on failure.
    """
    api_key = config.CONVERTHUB_API_KEY
    if not api_key:
        logger.warning("CONVERTHUB_API_KEY not set, skipping conversion")
        return None

    if not os.path.exists(ts_filepath):
        logger.error(f"File not found: {ts_filepath}")
        return None

    headers = {"Authorization": f"Bearer {api_key}"}
    mp4_filepath = os.path.splitext(ts_filepath)[0] + ".mp4"

    def _update(text: str):
        logger.info(text)
        if progress_callback:
            try:
                progress_callback(text)
            except Exception:
                pass

    try:
        # ── Step 1: Submit file for conversion ───────────────────────────
        _update("🔄 Converting .ts → .mp4...")
        file_size_mb = os.path.getsize(ts_filepath) / (1024 * 1024)
        logger.info(f"Uploading {ts_filepath} ({file_size_mb:.1f} MB) to ConvertHub")

        with open(ts_filepath, "rb") as f:
            resp = requests.post(
                f"{CONVERTHUB_BASE_URL}/convert",
                headers=headers,
                files={"file": (os.path.basename(ts_filepath), f, "video/mp2t")},
                data={"target_format": "mp4"},
                timeout=300,  # Upload timeout
            )

        if not resp.ok:
            logger.error(f"ConvertHub upload failed ({resp.status_code}): {resp.text}")
            return None

        job_data = resp.json()
        job_id = job_data.get("id") or job_data.get("job_id")
        if not job_id:
            logger.error(f"No job ID in response: {job_data}")
            return None

        logger.info(f"ConvertHub job submitted: {job_id}")

        # ── Step 2: Poll for completion ──────────────────────────────────
        start_time = time.time()
        while time.time() - start_time < timeout:
            time.sleep(poll_interval)

            status_resp = requests.get(
                f"{CONVERTHUB_BASE_URL}/jobs/{job_id}",
                headers=headers,
                timeout=30,
            )

            if not status_resp.ok:
                logger.warning(f"Status check failed: {status_resp.status_code}")
                continue

            status_data = status_resp.json()
            status = status_data.get("status", "").lower()
            progress = status_data.get("progress", "")

            if progress:
                _update(f"🔄 Converting .ts → .mp4... {progress}")

            if status in ("completed", "done", "finished", "success"):
                logger.info(f"Conversion complete: {job_id}")
                break
            elif status in ("failed", "error"):
                error_msg = status_data.get("error", "Unknown error")
                logger.error(f"Conversion failed: {error_msg}")
                return None
            # else: still processing, keep polling
        else:
            logger.error(f"Conversion timed out after {timeout}s")
            return None

        # ── Step 3: Download converted file ──────────────────────────────
        _update("⬇️ Downloading converted .mp4...")

        dl_resp = requests.get(
            f"{CONVERTHUB_BASE_URL}/jobs/{job_id}/download",
            headers=headers,
            timeout=300,
            stream=True,
        )

        if not dl_resp.ok:
            logger.error(f"Download failed ({dl_resp.status_code}): {dl_resp.text}")
            return None

        with open(mp4_filepath, "wb") as f:
            for chunk in dl_resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)

        if os.path.exists(mp4_filepath) and os.path.getsize(mp4_filepath) > 0:
            new_size_mb = os.path.getsize(mp4_filepath) / (1024 * 1024)
            logger.info(f"Converted: {mp4_filepath} ({new_size_mb:.1f} MB)")
            _update(f"✅ Converted to .mp4 ({new_size_mb:.1f} MB)")

            # Clean up original .ts file
            try:
                os.remove(ts_filepath)
                logger.info(f"Removed original .ts: {ts_filepath}")
            except OSError:
                pass

            return mp4_filepath
        else:
            logger.error("Downloaded file is empty or missing")
            return None

    except requests.exceptions.RequestException as e:
        logger.error(f"ConvertHub API error: {e}")
        return None
    except Exception as e:
        logger.error(f"Conversion error: {e}")
        return None
