"""
Adapter that wraps video_downloader.py for programmatic use.

Provides a clean API for downloading videos without going through
the CLI argparse interface.
"""
import os
import uuid
import logging

from src.video_downloader import (
    fetch_with_curl_cffi,
    fetch_with_playwright,
    extract_video_urls,
    download_m3u8_native,
    download_direct,
    generate_filename,
    VIDEO_EXTENSIONS,
)
from src import config

# Extensions/patterns that identify a URL as a direct video (skip scraping)
DIRECT_VIDEO_PATTERNS = (".m3u8", ".mp4", ".webm", ".mkv", ".ts", ".avi", ".flv", ".mov")

# Sites that yt-dlp handles better than our scraper
YTDLP_PREFERRED_DOMAINS = (
    "x.com", "twitter.com",
    "youtube.com", "youtu.be",
    "instagram.com",
    "facebook.com", "fb.watch",
    "tiktok.com",
    "vimeo.com",
    "dailymotion.com",
    "twitch.tv",
    "reddit.com",
)

logger = logging.getLogger(__name__)


def _try_ytdlp(url: str, output_path: str, progress_callback=None) -> dict | None:
    """
    Try downloading with yt-dlp. Returns result dict on success, None on failure.
    """
    try:
        import yt_dlp
    except ImportError:
        logger.warning("yt-dlp not installed, skipping yt-dlp fallback")
        return None

    logger.info(f"Trying yt-dlp for: {url[:80]}...")
    if progress_callback:
        try:
            progress_callback("⬇️ Downloading with yt-dlp...")
        except Exception:
            pass

    # Remove extension — yt-dlp adds its own
    output_template = os.path.splitext(output_path)[0] + ".%(ext)s"

    ydl_opts = {
        "outtmpl": output_template,
        "format": "best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                return None

            # Find the downloaded file
            filename = ydl.prepare_filename(info)
            # yt-dlp may change extension
            if not os.path.exists(filename):
                # Try .mp4
                filename = os.path.splitext(filename)[0] + ".mp4"
            if not os.path.exists(filename):
                # Search for any file matching the pattern
                base = os.path.splitext(output_template.replace("%(ext)s", ""))[0]
                import glob
                matches = glob.glob(base + ".*")
                if matches:
                    filename = matches[0]

            if os.path.exists(filename):
                size_mb = os.path.getsize(filename) / (1024 * 1024)
                return {
                    "filepath": filename,
                    "filename": os.path.basename(filename),
                    "size_mb": round(size_mb, 2),
                }
    except Exception as e:
        logger.warning(f"yt-dlp failed: {e}")

    return None


def download_video(
    url: str,
    download_dir: str = None,
    method: str = "auto",
    workers: int = 8,
    progress_callback=None,
) -> dict:
    """
    Download a video from the given URL.

    Args:
        url: The page URL containing the video.
        download_dir: Directory to save the downloaded file.
        method: Fetch method - "auto", "curl_cffi", or "playwright".
        workers: Number of threads for HLS downloads.
        progress_callback: Optional callable(status_text) for progress updates.

    Returns:
        dict with keys: filepath, filename, size_mb

    Raises:
        RuntimeError: On failure to fetch, extract, or download.
    """
    download_dir = download_dir or config.DOWNLOAD_DIR
    os.makedirs(download_dir, exist_ok=True)

    def _update(text: str):
        logger.info(text)
        if progress_callback:
            try:
                progress_callback(text)
            except Exception:
                pass  # Don't let callback errors break the download

    # ── Direct video URL fast-path (skip page scraping) ─────────────────
    url_lower = url.lower().split("?")[0]
    is_direct = any(url_lower.endswith(ext) for ext in DIRECT_VIDEO_PATTERNS)

    if is_direct:
        _update("🎯 Direct video URL detected, skipping page scrape...")
        unique_prefix = uuid.uuid4().hex[:8]
        filename = f"{unique_prefix}_{generate_filename(url)}"
        output_path = os.path.join(download_dir, filename)

        if ".m3u8" in url.lower():
            if not output_path.lower().endswith((".mp4", ".ts")):
                output_path = os.path.splitext(output_path)[0] + ".mp4"
            _update("⬇️ Downloading HLS stream...")
            success = download_m3u8_native(
                url, output_path, url, session=None, workers=workers
            )
        else:
            _update("⬇️ Downloading video...")
            download_direct(url, output_path, url, session=None)
            success = True

        if not success or not os.path.exists(output_path):
            ts_path = os.path.splitext(output_path)[0] + ".ts"
            if os.path.exists(ts_path):
                output_path = ts_path
            else:
                raise RuntimeError("Download completed but output file not found.")

        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        _update(f"✅ Download complete! ({size_mb:.1f} MB)")
        return {
            "filepath": output_path,
            "filename": os.path.basename(output_path),
            "size_mb": round(size_mb, 2),
        }

    # ── Try yt-dlp for known platforms (Twitter/X, YouTube, etc.) ────────
    from urllib.parse import urlparse
    domain = urlparse(url).netloc.lower().replace("www.", "")
    if any(d in domain for d in YTDLP_PREFERRED_DOMAINS):
        _update(f"🎬 Detected supported platform, trying yt-dlp...")
        unique_prefix = uuid.uuid4().hex[:8]
        output_path = os.path.join(download_dir, f"{unique_prefix}_video.mp4")
        result = _try_ytdlp(url, output_path, progress_callback=_update)
        if result:
            _update(f"✅ Download complete! ({result['size_mb']:.1f} MB)")
            return result
        _update("⚠️ yt-dlp failed, falling back to page scraping...")

    # ── Step 1: Fetch the page ───────────────────────────────────────────
    html = None
    session = None
    network_urls = []

    # Try Playwright first (better for embedded videos, captures real network requests)
    if method in ("auto", "playwright"):
        _update("🌐 Launching headless browser (Playwright)...")
        result = fetch_with_playwright(url)
        if result:
            html, network_urls = result

    # Fallback to curl_cffi if Playwright fails
    if html is None and method in ("auto", "curl_cffi"):
        _update("🌐 Fetching page with curl_cffi (TLS impersonation)...")
        html, session = fetch_with_curl_cffi(url)

    if html is None:
        raise RuntimeError(
            "Could not fetch the page. Ensure playwright is installed and "
            "Chromium browser is available, or try a direct video URL."
        )

    # ── Step 2: Extract video URLs ───────────────────────────────────────
    _update("🔎 Scanning page for video URLs...")
    video_urls = extract_video_urls(html, url)

    # Prepend network-captured URLs (from Playwright)
    for nurl in network_urls:
        if nurl not in [u for _, u in video_urls]:
            video_urls.insert(0, ("network-capture", nurl))

    downloadable = [(s, u) for s, u in video_urls if s != "iframe"]

    # Try iframes if no direct URLs found
    if not downloadable:
        iframes = [(s, u) for s, u in video_urls if s == "iframe"]
        for _, iframe_url in iframes:
            logger.info(f"Checking iframe: {iframe_url[:80]}...")
            try:
                if session:
                    resp = session.get(iframe_url, timeout=15)
                    if resp.status_code == 200:
                        extra = extract_video_urls(resp.text, iframe_url)
                        downloadable.extend(
                            [(s, u) for s, u in extra if s != "iframe"]
                        )
            except Exception as e:
                logger.warning(f"Iframe fetch failed: {e}")

    if not downloadable:
        # Last resort: try yt-dlp on the original URL
        _update("⚠️ No video URLs found via scraping, trying yt-dlp...")
        unique_prefix = uuid.uuid4().hex[:8]
        output_path = os.path.join(download_dir, f"{unique_prefix}_video.mp4")
        result = _try_ytdlp(url, output_path, progress_callback=_update)
        if result:
            _update(f"✅ Download complete! ({result['size_mb']:.1f} MB)")
            return result
        raise RuntimeError(
            "No downloadable video URLs found on the page. "
            "Try sending a direct video URL."
        )

    # ── Step 3: Pick best URL ────────────────────────────────────────────
    # Prefer network-captured URLs (real playback URLs)
    network_items = [(s, u) for s, u in downloadable if s == "network-capture"]
    other_items = [(s, u) for s, u in downloadable if s != "network-capture"]
    downloadable = network_items + other_items

    _, chosen_url = downloadable[0]
    logger.info(f"Selected URL: {chosen_url[:100]}...")

    # ── Step 4: Generate unique filename ─────────────────────────────────
    unique_prefix = uuid.uuid4().hex[:8]
    filename = f"{unique_prefix}_{generate_filename(chosen_url)}"
    output_path = os.path.join(download_dir, filename)

    # ── Step 5: Download ─────────────────────────────────────────────────
    _update(f"⬇️ Downloading video...")

    if ".m3u8" in chosen_url.lower():
        if not output_path.lower().endswith((".mp4", ".ts")):
            output_path = os.path.splitext(output_path)[0] + ".mp4"
        success = download_m3u8_native(
            chosen_url, output_path, url, session, workers=workers
        )
    else:
        download_direct(chosen_url, output_path, url, session)
        success = True

    if not success or not os.path.exists(output_path):
        # Check if .ts file was created instead (fallback merge)
        ts_path = os.path.splitext(output_path)[0] + ".ts"
        if os.path.exists(ts_path):
            output_path = ts_path
        else:
            # Last resort: try yt-dlp
            _update("⚠️ Scraper download failed, trying yt-dlp as last resort...")
            ytdlp_path = os.path.join(download_dir, f"{uuid.uuid4().hex[:8]}_video.mp4")
            result = _try_ytdlp(url, ytdlp_path, progress_callback=_update)
            if result:
                _update(f"✅ Download complete! ({result['size_mb']:.1f} MB)")
                return result
            raise RuntimeError("Download completed but output file not found.")

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    _update(f"✅ Download complete! ({size_mb:.1f} MB)")

    return {
        "filepath": output_path,
        "filename": os.path.basename(output_path),
        "size_mb": round(size_mb, 2),
    }
