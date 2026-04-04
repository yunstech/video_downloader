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
    "youtube.com", "youtu.be",
    "instagram.com",
    "facebook.com", "fb.watch",
    "tiktok.com",
    "vimeo.com",
    "dailymotion.com",
    "twitch.tv",
    "reddit.com",
)

# Twitter/X domains — handled by twittervideodownloader.com scraper first
TWITTER_DOMAINS = ("x.com", "twitter.com")

logger = logging.getLogger(__name__)


def _try_twitter_downloader(url: str, output_path: str, progress_callback=None) -> dict | None:
    """
    Download a Twitter/X video via twittervideodownloader.com.
    Properly handles Django CSRF + gql token, picks highest quality mp4.
    """
    import re
    import requests as req

    def _update(text):
        logger.info(text)
        if progress_callback:
            try:
                progress_callback(text)
            except Exception:
                pass

    _update("🐦 Fetching video link via twittervideodownloader.com...")

    try:
        session = req.Session()
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://twittervideodownloader.com/",
            "Origin": "https://twittervideodownloader.com",
        }

        # Step 1: GET homepage to obtain session cookie + form tokens
        logger.debug("GET https://twittervideodownloader.com/")
        page_resp = session.get(
            "https://twittervideodownloader.com/", headers=headers, timeout=15
        )
        logger.debug(f"Homepage status: {page_resp.status_code}, cookies: {dict(session.cookies)}")
        page_resp.raise_for_status()

        # Extract ALL hidden form fields (csrfmiddlewaretoken + gql)
        tokens = {
            k: v for k, v in re.findall(
                r'<input[^>]+name=["\']([^"\']+)["\'][^>]+value=["\']([^"\']*)["\']',
                page_resp.text
            )
        }
        logger.debug(f"Form tokens found: {list(tokens.keys())}")
        if not tokens.get("csrfmiddlewaretoken"):
            logger.warning("twittervideodownloader.com: no CSRF token found")
            return None

        # Step 2: POST tweet URL with all tokens
        post_headers = {
            **headers,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CSRFToken": tokens["csrfmiddlewaretoken"],
        }
        post_data = {"tweet": url, **tokens}
        logger.debug(f"POST /download with tweet={url}, token_keys={list(tokens.keys())}")

        resp = session.post(
            "https://twittervideodownloader.com/download",
            data=post_data,
            headers=post_headers,
            timeout=20,
        )
        logger.debug(f"POST status: {resp.status_code}, final_url: {resp.url}")
        resp.raise_for_status()

        # Step 3: Extract all video.twimg.com mp4 links from href attributes
        video_links = re.findall(
            r'href=["\']( https?://video\.twimg\.com/[^"\']+\.mp4[^"\']*)["\']|'
            r'href=["\'](https?://video\.twimg\.com/[^"\']+\.mp4[^"\']*)["\']',
            resp.text, re.IGNORECASE
        )
        # Flatten and clean
        links = [
            (a or b).strip()
            for a, b in video_links
            if (a or b).strip()
        ]
        logger.debug(f"Extracted {len(links)} video link(s): {links}")

        if not links:
            logger.warning("twittervideodownloader.com: no video links in response")
            return None

        # Pick highest quality = last link (they're ordered low → high)
        chosen = links[-1]
        logger.info(f"Downloading ({len(links)} qualities, best): {chosen[:100]}")
        _update(f"⬇️ Downloading Twitter video ({len(links)} qualities available)...")

        # Step 4: Download the mp4
        logger.debug(f"GET video: {chosen[:120]}")
        dl_resp = session.get(
            chosen,
            headers={
                "User-Agent": headers["User-Agent"],
                "Referer": "https://twittervideodownloader.com/",
            },
            stream=True,
            timeout=120,
        )
        logger.debug(f"Video download status: {dl_resp.status_code}, content-type: {dl_resp.headers.get('content-type', '?')}")
        dl_resp.raise_for_status()

        bytes_written = 0
        with open(output_path, "wb") as f:
            for chunk in dl_resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    bytes_written += len(chunk)
        logger.debug(f"Wrote {bytes_written / 1024 / 1024:.2f} MB to {output_path}")

        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            return {
                "filepath": output_path,
                "filename": os.path.basename(output_path),
                "size_mb": round(size_mb, 2),
            }

    except Exception as e:
        logger.warning(f"twittervideodownloader.com failed: {e}", exc_info=True)

    return None


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
    logger.debug(f"yt-dlp output template: {output_template}")

    def _ytdlp_progress_hook(d):
        status = d.get("status", "?")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0
            eta = d.get("eta") or 0
            pct = (downloaded / total * 100) if total else 0
            logger.debug(
                f"yt-dlp downloading: {pct:.1f}% "
                f"({downloaded // 1024 // 1024} MB / {total // 1024 // 1024} MB) "
                f"speed={speed // 1024:.0f} KB/s eta={eta}s"
            )
        elif status == "finished":
            fname = d.get("filename", "?")
            logger.debug(f"yt-dlp finished fragment/file: {fname}")
        elif status == "error":
            logger.warning(f"yt-dlp hook reported error: {d}")

    ydl_opts = {
        "outtmpl": output_template,
        "format": "best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": False,
        "no_warnings": False,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 3,
        "progress_hooks": [_ytdlp_progress_hook],
        "logger": logging.getLogger("yt_dlp"),
    }

    # Use cookies file for Twitter/X if available (needed for auth-gated content)
    from urllib.parse import urlparse as _urlparse
    _domain = _urlparse(url).netloc.lower()
    if any(d in _domain for d in ("x.com", "twitter.com")):
        cookies_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "twitter_cookies.txt")
        if os.path.exists(cookies_file):
            ydl_opts["cookiefile"] = cookies_file
            logger.info("yt-dlp: using twitter_cookies.txt")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.debug(f"yt-dlp extract_info start: {url}")
            try:
                info = ydl.extract_info(url, download=True)
            except Exception as inner_e:
                # Re-raise so the caller gets the exact yt-dlp error message
                raise inner_e
            if info is None:
                logger.warning("yt-dlp: extract_info returned None")
                return None

            logger.debug(
                f"yt-dlp info: extractor={info.get('extractor','?')} "
                f"title={str(info.get('title','?'))[:60]} "
                f"ext={info.get('ext','?')} "
                f"format={info.get('format','?')}"
            )

            # Find the downloaded file
            filename = ydl.prepare_filename(info)
            logger.debug(f"yt-dlp prepare_filename: {filename}")
            # yt-dlp may change extension
            if not os.path.exists(filename):
                # Try .mp4
                filename = os.path.splitext(filename)[0] + ".mp4"
            if not os.path.exists(filename):
                # Search for any file matching the pattern
                base = os.path.splitext(output_template.replace("%(ext)s", ""))[0]
                import glob
                matches = glob.glob(base + ".*")
                logger.debug(f"yt-dlp glob search '{base}.*' found: {matches}")
                if matches:
                    filename = matches[0]

            if os.path.exists(filename):
                size_mb = os.path.getsize(filename) / (1024 * 1024)
                logger.info(f"yt-dlp success: {filename} ({size_mb:.2f} MB)")
                return {
                    "filepath": filename,
                    "filename": os.path.basename(filename),
                    "size_mb": round(size_mb, 2),
                }
            else:
                logger.warning(f"yt-dlp: expected output file not found: {filename}")
    except Exception as e:
        logger.warning(f"yt-dlp failed: {e}", exc_info=True)
        raise  # re-raise so callers can inspect the error

    return None


def download_video(
    url: str,
    download_dir: str = None,
    method: str = "auto",
    workers: int = 4,
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

    logger.debug(f"download_video called: url={url}, method={method}, workers={workers}")

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
    logger.debug(f"is_direct={is_direct}, url_lower={url_lower}")

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

    # ── Twitter/X: use twittervideodownloader.com ────────────────────────
    from urllib.parse import urlparse
    domain = urlparse(url).netloc.lower().replace("www.", "")
    logger.debug(f"Parsed domain: {domain}")

    if any(d in domain for d in TWITTER_DOMAINS):
        _update("🐦 Twitter/X URL detected, trying yt-dlp...")
        unique_prefix = uuid.uuid4().hex[:8]
        output_path = os.path.join(download_dir, f"{unique_prefix}_twitter.mp4")
        try:
            result = _try_ytdlp(url, output_path, progress_callback=_update)
            if result:
                _update(f"✅ Download complete! ({result['size_mb']:.1f} MB)")
                return result
            raise RuntimeError("yt-dlp returned no result")
        except Exception as e:
            err_str = str(e).lower()
            if "no video" in err_str or "no formats" in err_str:
                raise RuntimeError("This tweet does not contain a video.")
            elif "login" in err_str or "auth" in err_str or "age" in err_str or "private" in err_str:
                raise RuntimeError(
                    "Twitter/X requires login to download this video. "
                    "Add a twitter_cookies.txt file to the bot's data directory."
                )
            else:
                raise RuntimeError(f"Twitter/X download failed: {str(e).splitlines()[0]}")

    # ── Try yt-dlp for other known platforms (YouTube, Instagram, etc.) ──
    elif any(d in domain for d in YTDLP_PREFERRED_DOMAINS):
        _update(f"🎬 Detected supported platform, trying yt-dlp...")
        unique_prefix = uuid.uuid4().hex[:8]
        output_path = os.path.join(download_dir, f"{unique_prefix}_video.mp4")
        result = _try_ytdlp(url, output_path, progress_callback=_update)
        if result:
            _update(f"✅ Download complete! ({result['size_mb']:.1f} MB)")
            return result
        _update("⚠️ yt-dlp failed, falling back to page scraping...")
    else:
        logger.debug(f"Domain '{domain}' not in Twitter or preferred yt-dlp list, using scraper")

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
            logger.debug(f"Playwright: got {len(html)} chars HTML, {len(network_urls)} network URLs captured")
        else:
            logger.debug("Playwright: returned no result")

    # Fallback to curl_cffi if Playwright fails
    if html is None and method in ("auto", "curl_cffi"):
        _update("🌐 Fetching page with curl_cffi (TLS impersonation)...")
        html, session = fetch_with_curl_cffi(url)
        if html:
            logger.debug(f"curl_cffi: got {len(html)} chars HTML")
        else:
            logger.debug("curl_cffi: returned no HTML")

    if html is None:
        raise RuntimeError(
            "Could not fetch the page. Ensure playwright is installed and "
            "Chromium browser is available, or try a direct video URL."
        )

    # ── Step 2: Extract video URLs ───────────────────────────────────────
    _update("🔎 Scanning page for video URLs...")
    video_urls = extract_video_urls(html, url)
    logger.debug(f"extract_video_urls returned {len(video_urls)} URL(s): {[(s, u[:80]) for s, u in video_urls]}")

    # Prepend network-captured URLs (from Playwright)
    for nurl in network_urls:
        if nurl not in [u for _, u in video_urls]:
            video_urls.insert(0, ("network-capture", nurl))
    logger.debug(f"After network-capture merge: {len(video_urls)} URL(s) total")

    downloadable = [(s, u) for s, u in video_urls if s != "iframe"]
    logger.debug(f"Downloadable (non-iframe): {len(downloadable)} — {[(s, u[:80]) for s, u in downloadable]}")

    # Try iframes if no direct URLs found
    if not downloadable:
        iframes = [(s, u) for s, u in video_urls if s == "iframe"]
        logger.debug(f"No direct URLs; checking {len(iframes)} iframe(s)")
        for _, iframe_url in iframes:
            logger.info(f"Checking iframe: {iframe_url[:80]}...")
            try:
                if session:
                    resp = session.get(iframe_url, timeout=15)
                    logger.debug(f"Iframe fetch status: {resp.status_code}")
                    if resp.status_code == 200:
                        extra = extract_video_urls(resp.text, iframe_url)
                        logger.debug(f"Iframe yielded {len(extra)} extra URL(s)")
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
