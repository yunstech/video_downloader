"""
YouTube Live Stream Recorder

Records YouTube live/membership streams at 480p with H.264 CRF-28 encoding
to keep file size small for 10+ hour recordings.

Features:
- Cookies support for membership-only streams
- 480p quality cap
- CRF-28 re-encode (≈300-500 kbps) for minimal storage
- Graceful stop via Redis flag
- Auto-splits files > 1.9 GB into 2-hour parts
"""
import os
import time
import uuid
import logging
import subprocess

logger = logging.getLogger(__name__)

# Redis key templates
RECORD_STOP_KEY = "record_stop:{user_id}"
RECORD_PID_KEY = "record_pid:{user_id}"

# Max file size before auto-splitting (bytes)
MAX_PART_SIZE = 1900 * 1024 * 1024  # 1.9 GB

# Progress heartbeat interval (seconds)
HEARTBEAT_INTERVAL = 120  # 2 minutes


def record_youtube_live(
    url: str,
    output_dir: str,
    user_id: int,
    cookies_path: str = None,
    live_from_start: bool = False,
    progress_callback=None,
    redis_conn=None,
) -> dict:
    """
    Record a YouTube live stream at 480p with CRF-28 encoding.

    Args:
        url:              YouTube live URL (can be membership-locked with cookies).
        output_dir:       Directory to save the recording.
        user_id:          Telegram user ID (used for Redis stop/pid keys).
        cookies_path:     Path to Netscape-format cookies.txt (for memberships).
        live_from_start:  If True, record from the beginning of DVR (--live-from-start).
        progress_callback: Callable(str) for status updates.
        redis_conn:       Redis connection for stop-flag polling.

    Returns:
        dict:
            filepaths      — list of output .mp4 file paths
            duration_mins  — total recording wall-clock minutes
            stopped_by_user — True if user sent /stoprecord
    """
    os.makedirs(output_dir, exist_ok=True)

    unique_id = uuid.uuid4().hex[:8]
    output_template = os.path.join(output_dir, f"live_{unique_id}_%(title).100B.%(ext)s")

    def _update(text: str):
        logger.info(text)
        if progress_callback:
            try:
                progress_callback(text)
            except Exception:
                pass

    _update("⏺️ Preparing YouTube live recorder…")

    # ── Build yt-dlp command ─────────────────────────────────────────────────
    cmd = ["yt-dlp"]

    # Cookies for membership streams
    if cookies_path and os.path.isfile(cookies_path):
        cmd += ["--cookies", cookies_path]
        logger.info(f"Using cookies from {cookies_path}")

    # Format: prefer H.264 ≤480p + M4A audio for maximum compatibility
    # Falls back to any 480p, then best available
    cmd += [
        "--format",
        (
            "bestvideo[height<=480][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
            "bestvideo[height<=480][vcodec^=avc1]+bestaudio/"
            "bestvideo[height<=480]+bestaudio/"
            "best[height<=480]/best"
        ),
        "--merge-output-format", "mp4",
    ]

    # Re-encode for small file size:
    # - scale to 480p (handles cases where yt-dlp picks 360p video + 480 audio)
    # - libx264 CRF 28 = visually decent, ~300-500 kbps at 480p
    # - AAC 64 kbps audio (sufficient for voice/music in a live stream)
    # - faststart moves moov atom to front so partial downloads are playable
    cmd += [
        "--postprocessor-args",
        (
            "ffmpeg:"
            "-vf scale=-2:480 "
            "-c:v libx264 -crf 28 -preset fast "
            "-c:a aac -b:a 64k "
            "-movflags +faststart"
        ),
    ]

    # Live stream DVR option
    if live_from_start:
        cmd += ["--live-from-start"]

    # Misc options
    cmd += [
        "--no-keep-fragments",
        "--no-playlist",
        "--newline",          # One progress line per segment — easier to parse
        "--output", output_template,
        url,
    ]

    logger.info(f"yt-dlp cmd: {' '.join(cmd)}")

    start_time = time.time()
    proc = None
    stopped_by_user = False

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        # Store PID so we can kill from outside if needed
        if redis_conn:
            redis_conn.set(
                RECORD_PID_KEY.format(user_id=user_id),
                proc.pid,
                ex=86400,  # TTL: 24 hours
            )

        last_heartbeat = time.time()
        recent_lines: list[str] = []

        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            if not line:
                continue

            recent_lines.append(line)
            if len(recent_lines) > 20:
                recent_lines.pop(0)

            logger.debug(f"yt-dlp: {line}")

            # Poll Redis stop flag every output line (cheap)
            if redis_conn:
                stop_key = RECORD_STOP_KEY.format(user_id=user_id)
                if redis_conn.exists(stop_key):
                    logger.info(f"Stop flag set for user {user_id} — terminating yt-dlp")
                    proc.terminate()
                    redis_conn.delete(stop_key)
                    stopped_by_user = True
                    break

            # Heartbeat every 2 minutes
            if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
                elapsed = int(time.time() - start_time)
                h, m = divmod(elapsed // 60, 60)
                _update(
                    f"⏺️ Recording… {h}h {m:02d}m elapsed\n"
                    f"`{line[:120]}`"
                )
                last_heartbeat = time.time()

        proc.wait()
        returncode = proc.returncode

    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if redis_conn:
            redis_conn.delete(RECORD_PID_KEY.format(user_id=user_id))

    # yt-dlp returns 0 on success; -15 = SIGTERM (user stop)
    if returncode not in (0, None) and not stopped_by_user:
        error_snippet = "\n".join(recent_lines[-8:]) if recent_lines else "unknown error"
        raise RuntimeError(
            f"yt-dlp exited with code {returncode}:\n{error_snippet[-400:]}"
        )

    duration_mins = max(1, int((time.time() - start_time) / 60))

    # ── Collect output files ─────────────────────────────────────────────────
    files: list[str] = []
    for fname in sorted(os.listdir(output_dir)):
        if fname.startswith(f"live_{unique_id}_") and fname.endswith(".mp4"):
            files.append(os.path.join(output_dir, fname))

    if not files:
        raise RuntimeError("Recording finished but no output file was found.")

    # ── Split oversized files ────────────────────────────────────────────────
    final_files: list[str] = []
    for fpath in files:
        if os.path.getsize(fpath) > MAX_PART_SIZE:
            _update(
                f"✂️ File is {os.path.getsize(fpath) / 1024**3:.1f} GB — "
                "splitting into 2-hour parts…"
            )
            parts = _split_video(fpath, output_dir)
            final_files.extend(parts)
            try:
                os.remove(fpath)
            except OSError:
                pass
        else:
            final_files.append(fpath)

    return {
        "filepaths": final_files,
        "duration_mins": duration_mins,
        "stopped_by_user": stopped_by_user,
    }


def _split_video(filepath: str, output_dir: str, segment_minutes: int = 120) -> list[str]:
    """
    Split *filepath* into segments of *segment_minutes* using stream-copy.
    Returns a sorted list of part file paths.
    Falls back to the original file if ffmpeg fails.
    """
    base = os.path.splitext(os.path.basename(filepath))[0]
    pattern = os.path.join(output_dir, f"{base}_part%03d.mp4")

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", filepath,
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(segment_minutes * 60),
        "-reset_timestamps", "1",
        pattern,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        logger.error(f"ffmpeg split failed (rc={result.returncode}): {result.stderr[:300]}")
        return [filepath]

    parts = sorted(
        os.path.join(output_dir, f)
        for f in os.listdir(output_dir)
        if os.path.basename(f).startswith(base + "_part") and f.endswith(".mp4")
    )
    return parts if parts else [filepath]
