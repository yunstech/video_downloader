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

logger = logging.getLogger(__name__)


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

    # ── Step 1: Fetch the page ───────────────────────────────────────────
    html = None
    session = None
    network_urls = []

    if method in ("auto", "curl_cffi"):
        _update("⬇️ Fetching page with curl_cffi...")
        html, session = fetch_with_curl_cffi(url)

    if html is None and method in ("auto", "playwright"):
        _update("⬇️ Fetching page with Playwright...")
        result = fetch_with_playwright(url)
        if result:
            html, network_urls = result

    if html is None:
        raise RuntimeError(
            "Could not fetch the page. Possible Cloudflare block. "
            "Ensure curl_cffi is installed."
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
        raise RuntimeError(
            "No downloadable video URLs found on the page. "
            "Try using --method playwright or provide a direct URL."
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
            raise RuntimeError("Download completed but output file not found.")

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    _update(f"✅ Download complete! ({size_mb:.1f} MB)")

    return {
        "filepath": output_path,
        "filename": os.path.basename(output_path),
        "size_mb": round(size_mb, 2),
    }
