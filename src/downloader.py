"""
Adapter that wraps video_downloader.py for programmatic use.

Provides a clean API for downloading videos without going through
the CLI argparse interface.
"""
import os
import re
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
from src.terabox import TeraboxDownloader

# Minimum file size in bytes to consider a download valid (100 KB)
# Anything smaller is likely an error page, empty file, or ad stub
MIN_VIDEO_FILE_SIZE = 100 * 1024

# Extensions/patterns that identify a URL as a direct video (skip scraping)
DIRECT_VIDEO_PATTERNS = (".m3u8", ".mp4", ".webm", ".mkv", ".ts", ".avi", ".flv", ".mov")

# Sites that yt-dlp handles well (no ad issues)
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

# Adult sites — use Playwright scraper instead of yt-dlp
# yt-dlp downloads ads on these sites
ADULT_PAGE_DOMAINS = (
    "pornhub.com",
    "xvideos.com",
    "xhamster.com",
    "redtube.com",
    "youporn.com",
    "spankbang.com",
    "eporner.com",
    "xnxx.com",
    "tube8.com",
    "beeg.com",
)

# CDN domains for adult sites — direct video URLs that need proper headers
ADULT_CDN_DOMAINS = (
    "phncdn.com",       # PornHub CDN
    "cdn13.com",        # XVideos CDN
    "xhcdn.com",        # xHamster CDN
    "rdtcdn.com",       # RedTube CDN
)


# Twitter/X domains
TWITTER_DOMAINS = ("x.com", "twitter.com")

# Terabox domains
TERABOX_DOMAINS = (
    "terabox.com", "1024terabox.com", "freeterabox.com", "nephobox.com",
    "terabox.app", "teraboxapp.com", "4funbox.com", "mirrobox.com",
    "momerybox.com", "teraboxlink.com",
)

# Map CDN domains to their parent site referer
CDN_REFERER_MAP = {
    "phncdn.com": "https://www.pornhub.com/",
    "cdn13.com": "https://www.xvideos.com/",
    "xhcdn.com": "https://www.xhamster.com/",
    "rdtcdn.com": "https://www.redtube.com/",
}

# Known ad URL patterns to filter out from scraped results
AD_URL_PATTERNS = (
    "trafficjunky.com",
    "trafficjunky.net",
    "adskeeper.com",
    "adspyglass.com",
    "juicyads.com",
    "exoclick.com",
    "exosrv.com",
    "adxxx.com",
    "tsyndicate.com",
    "syndication.com",
    "popads.net",
    "plugrush.com",
    "ad.doubleclick.net",
    "/ad/",
    "/ads/",
    "/adserver/",
    "tracking.php",
    "click.php",
)

logger = logging.getLogger(__name__)


def _validate_downloaded_file(filepath: str) -> dict:
    """
    Validate a downloaded file and return result dict.
    Raises RuntimeError if the file is missing, empty, or too small.
    """
    if not os.path.exists(filepath):
        raise RuntimeError("Download completed but output file not found.")

    file_size = os.path.getsize(filepath)
    size_mb = file_size / (1024 * 1024)

    if file_size == 0:
        # Clean up empty file
        os.remove(filepath)
        raise RuntimeError(
            "Downloaded file is empty (0 bytes). The video URL may have expired "
            "or the server rejected the request. Try sending the original page URL."
        )

    if file_size < MIN_VIDEO_FILE_SIZE:
        # Check if it's an HTML error page disguised as a video
        try:
            with open(filepath, "rb") as f:
                header = f.read(512)
            if b"<html" in header.lower() or b"<!doctype" in header.lower():
                os.remove(filepath)
                raise RuntimeError(
                    "Server returned an HTML page instead of a video. "
                    "The video URL has likely expired. "
                    "Try sending the original page URL."
                )
        except RuntimeError:
            raise
        except Exception:
            pass

        os.remove(filepath)
        raise RuntimeError(
            f"Downloaded file is too small ({file_size} bytes). "
            "This is likely an error response, not a video. "
            "Try sending the original page URL."
        )

    logger.info(f"Validated download: {filepath} ({size_mb:.2f} MB)")
    return {
        "filepath": filepath,
        "filename": os.path.basename(filepath),
        "size_mb": round(size_mb, 2),
    }


def _get_cdn_referer(domain: str) -> str:
    """Get the appropriate Referer header for a CDN domain."""
    for cdn_domain, referer in CDN_REFERER_MAP.items():
        if cdn_domain in domain:
            return referer
    return ""


def _is_ad_url(url: str) -> bool:
    """Check if a URL looks like an ad/tracking URL."""
    url_lower = url.lower()
    return any(pattern in url_lower for pattern in AD_URL_PATTERNS)


def _filter_ad_urls(video_urls: list) -> list:
    """Remove ad/tracking URLs from a list of (source, url) tuples."""
    filtered = []
    for source, url in video_urls:
        if _is_ad_url(url):
            logger.debug(f"Filtered ad URL: {url[:80]}")
        else:
            filtered.append((source, url))
    if len(filtered) < len(video_urls):
        logger.info(f"Filtered {len(video_urls) - len(filtered)} ad URL(s)")
    return filtered


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

        if os.path.exists(output_path) and os.path.getsize(output_path) >= MIN_VIDEO_FILE_SIZE:
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            return {
                "filepath": output_path,
                "filename": os.path.basename(output_path),
                "size_mb": round(size_mb, 2),
            }
        elif os.path.exists(output_path):
            logger.warning(f"Twitter download too small ({os.path.getsize(output_path)} bytes)")
            os.remove(output_path)

    except Exception as e:
        logger.warning(f"twittervideodownloader.com failed: {e}", exc_info=True)

    return None


# ── Vidara.to / Vidara.so handler ────────────────────────────────────────────

VIDARA_DOMAINS = ("vidara.to", "vidara.so")


def _try_vidara(url: str, output_path: str, progress_callback=None) -> dict | None:
    """
    Download a video from Vidara.to/Vidara.so by calling their stream API.

    Flow:
    1. Extract filecode from URL (vidara.to/v/<code> or vidara.so/e/<code>)
    2. POST to Vidara.so/api/stream with filecode → get master m3u8 URL
    3. Download the full HLS stream via download_m3u8_native()
    """
    import re
    from curl_cffi import requests as cffi_req

    def _update(text):
        logger.info(text)
        if progress_callback:
            try:
                progress_callback(text)
            except Exception:
                pass

    # Extract filecode from URL
    # Patterns: vidara.to/v/<code>  or  vidara.so/e/<code>  or  vidara.to/<code>
    match = re.search(r'vidara\.(?:to|so)/(?:v|e|d)/([A-Za-z0-9]+)', url)
    if not match:
        match = re.search(r'vidara\.(?:to|so)/([A-Za-z0-9]{8,})', url)
    if not match:
        logger.warning(f"Vidara: could not extract filecode from URL: {url}")
        return None

    filecode = match.group(1)
    logger.info(f"Vidara filecode: {filecode}")
    _update("🎬 Vidara detected — fetching stream info...")

    try:
        session = cffi_req.Session(impersonate="chrome")
        headers = {
            "Referer": "https://Vidara.so/",
            "Content-Type": "application/json",
        }

        # Call the stream API
        resp = session.post(
            "https://Vidara.so/api/stream",
            headers=headers,
            json={"filecode": filecode, "device": "web"},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()

        streaming_url = data.get("streaming_url")
        title = data.get("title", filecode)
        logger.debug(f"Vidara API response: title={title}, streaming_url={streaming_url!r}")

        if not streaming_url:
            logger.warning("Vidara: API returned no streaming_url")
            return None

        _update(f"⬇️ Downloading: {title}")

        # Ensure output path ends in .mp4
        if not output_path.lower().endswith((".mp4", ".ts")):
            output_path = os.path.splitext(output_path)[0] + ".mp4"

        # Download the full HLS stream using our native downloader
        success = download_m3u8_native(
            streaming_url,
            output_path,
            referer="https://Vidara.so/",
            session=None,
            workers=4,
        )

        if not success or not os.path.exists(output_path):
            # Check for .ts fallback
            ts_path = os.path.splitext(output_path)[0] + ".ts"
            if os.path.exists(ts_path):
                output_path = ts_path
            else:
                logger.warning("Vidara: HLS download failed")
                return None

        return _validate_downloaded_file(output_path)

    except Exception as e:
        logger.warning(f"Vidara download failed: {e}", exc_info=True)

    return None


def _try_ytdlp(url: str, output_path: str, progress_callback=None) -> dict | None:
    """
    Try downloading with yt-dlp. Returns result dict on success, None on failure.
    Includes ad filtering: skips videos < 30s and videos with ad-like titles.
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

    def _is_not_ad(info_dict, *, incomplete):
        """Filter out short videos (likely pre-roll ads) and ad-titled videos."""
        duration = info_dict.get("duration")
        if duration is not None and duration < 30:
            logger.info(f"yt-dlp: skipping short video ({duration}s, likely ad)")
            return "Skipping short video (likely an ad)"
        title = (info_dict.get("title") or "").lower()
        ad_keywords = (
            "sponsor", "advertisement", "promo", "ad -", "advert",
            "commercial", "promoted", "- ad", "[ad]", "(ad)",
        )
        if any(kw in title for kw in ad_keywords):
            logger.info(f"yt-dlp: skipping ad-titled video: {title[:60]}")
            return "Skipping advertisement"
        # Check the URL itself for ad patterns
        video_url = info_dict.get("url") or info_dict.get("webpage_url") or ""
        if _is_ad_url(video_url):
            logger.info(f"yt-dlp: skipping ad URL: {video_url[:80]}")
            return "Skipping ad URL"
        return None

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
        "match_filter": _is_not_ad,
        "noplaylist": True,
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
                raise inner_e
            if info is None:
                logger.warning("yt-dlp: extract_info returned None")
                return None

            logger.debug(
                f"yt-dlp info: extractor={info.get('extractor','?')} "
                f"title={str(info.get('title','?'))[:60]} "
                f"ext={info.get('ext','?')} "
                f"format={info.get('format','?')} "
                f"duration={info.get('duration','?')}"
            )

            # Double-check duration after download (safety net)
            duration = info.get("duration")
            if duration is not None and duration < 30:
                logger.warning(f"yt-dlp: downloaded video is only {duration}s — likely an ad, discarding")
                # Clean up the ad file
                filename = ydl.prepare_filename(info)
                if os.path.exists(filename):
                    os.remove(filename)
                    logger.debug(f"Removed ad file: {filename}")
                mp4_path = os.path.splitext(filename)[0] + ".mp4"
                if os.path.exists(mp4_path):
                    os.remove(mp4_path)
                return None

            # Find the downloaded file
            filename = ydl.prepare_filename(info)
            logger.debug(f"yt-dlp prepare_filename: {filename}")
            # yt-dlp may change extension
            if not os.path.exists(filename):
                filename = os.path.splitext(filename)[0] + ".mp4"
            if not os.path.exists(filename):
                base = os.path.splitext(output_template.replace("%(ext)s", ""))[0]
                import glob
                matches = glob.glob(base + ".*")
                logger.debug(f"yt-dlp glob search '{base}.*' found: {matches}")
                if matches:
                    filename = matches[0]

            if os.path.exists(filename):
                size_mb = os.path.getsize(filename) / (1024 * 1024)
                # Another ad heuristic: if file is tiny (< 1 MB), likely an ad
                if size_mb < 1.0:
                    logger.warning(f"yt-dlp: downloaded file is only {size_mb:.2f} MB — likely an ad, discarding")
                    os.remove(filename)
                    return None
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
        raise

    return None


def _download_direct_with_headers(
    url: str,
    output_path: str,
    referer: str = "",
    progress_callback=None,
) -> dict | None:
    """
    Download a direct video URL with proper browser-like headers.
    Handles CDN URLs that require Referer/User-Agent to avoid 403.
    Supports large files with streaming and progress logging.
    """
    import requests as req

    def _update(text):
        logger.info(text)
        if progress_callback:
            try:
                progress_callback(text)
            except Exception:
                pass

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Encoding": "identity",  # avoid gzip for video streams
        "Connection": "keep-alive",
    }
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = referer.rstrip("/")

    try:
        _update("⬇️ Downloading video (direct)...")
        logger.debug(f"Direct download: {url[:120]}")
        logger.debug(f"Headers: Referer={referer}")

        resp = req.get(url, headers=headers, stream=True, timeout=120)
        logger.debug(
            f"Response: status={resp.status_code}, "
            f"content-type={resp.headers.get('content-type', '?')}, "
            f"content-length={resp.headers.get('content-length', '?')}"
        )
        resp.raise_for_status()

        # Check content-type — if we got HTML, the token probably expired
        content_type = resp.headers.get("content-type", "").lower()
        if "text/html" in content_type:
            logger.warning("Got HTML response instead of video — token likely expired")
            return None

        total_bytes = int(resp.headers.get("content-length", 0))
        bytes_written = 0

        with open(output_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    bytes_written += len(chunk)
                    if total_bytes and bytes_written % (1024 * 1024 * 5) < 65536:
                        pct = bytes_written / total_bytes * 100
                        logger.debug(
                            f"Progress: {pct:.1f}% "
                            f"({bytes_written // 1024 // 1024} MB / "
                            f"{total_bytes // 1024 // 1024} MB)"
                        )

        logger.debug(f"Wrote {bytes_written / 1024 / 1024:.2f} MB to {output_path}")

        if os.path.exists(output_path) and os.path.getsize(output_path) >= MIN_VIDEO_FILE_SIZE:
            # Check that we didn't download an HTML error page
            with open(output_path, "rb") as f:
                header = f.read(64)
            if b"<html" in header.lower() or b"<!doctype" in header.lower():
                logger.warning("Downloaded file is HTML, not video — removing")
                os.remove(output_path)
                return None

            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            return {
                "filepath": output_path,
                "filename": os.path.basename(output_path),
                "size_mb": round(size_mb, 2),
            }
        elif os.path.exists(output_path):
            file_size = os.path.getsize(output_path)
            logger.warning(f"Downloaded file too small ({file_size} bytes), removing")
            os.remove(output_path)
            return None

    except req.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        logger.warning(f"Direct download HTTP error {status}: {e}")
        if status == 403:
            logger.info("403 Forbidden — CDN token likely expired or bad Referer")
        elif status == 410:
            logger.info("410 Gone — CDN token has expired")
        return None
    except Exception as e:
        logger.warning(f"Direct download failed: {e}", exc_info=True)
        return None

    return None


def _download_m3u8_ffmpeg(
    m3u8_url: str,
    output_path: str,
    referer: str = "",
    progress_callback=None,
) -> bool:
    """
    Download an HLS stream using ffmpeg.
    Much more reliable than custom segment downloading — ffmpeg handles:
    - Variant/master playlists (auto-selects best quality)
    - Segment retries and reassembly
    - Proper muxing to mp4
    Returns True on success, False on failure.
    """
    import subprocess
    import shutil

    def _update(text):
        logger.info(text)
        if progress_callback:
            try:
                progress_callback(text)
            except Exception:
                pass

    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        logger.warning("ffmpeg not found in PATH, cannot use ffmpeg for HLS")
        return False

    # Ensure output is .mp4
    if not output_path.lower().endswith(".mp4"):
        output_path = os.path.splitext(output_path)[0] + ".mp4"

    _update("⬇️ Downloading HLS stream with ffmpeg...")

    # Build ffmpeg command
    cmd = [
        ffmpeg_path,
        "-y",  # overwrite output
        # Essential for HLS streams — allow all required protocols
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
        # Handle connection drops (common with CDN-served HLS)
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
    ]

    # Add headers
    headers_str = (
        f"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/120.0.0.0 Safari/537.36\r\n"
    )
    if referer:
        headers_str += f"Referer: {referer}\r\n"
        headers_str += f"Origin: {referer.rstrip('/')}\r\n"

    cmd.extend([
        "-headers", headers_str,
        "-i", m3u8_url,
        "-c", "copy",           # no re-encoding, just copy streams
        "-bsf:a", "aac_adtstoasc",  # fix AAC stream for mp4 container
        "-movflags", "+faststart",   # enable streaming-friendly mp4
        "-max_muxing_queue_size", "1024",  # prevent queue overflow on long videos
        "-loglevel", "warning",
        output_path,
    ])

    logger.debug(f"ffmpeg cmd: {' '.join(cmd[:6])}... {output_path}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute timeout for long videos
        )

        if result.stderr:
            logger.debug(f"ffmpeg stderr: {result.stderr.strip()[:500]}")

        if result.returncode != 0:
            stderr = result.stderr.strip()
            logger.warning(f"ffmpeg failed (rc={result.returncode}): {stderr[:200]}")
            # Clean up partial file
            if os.path.exists(output_path):
                os.remove(output_path)
            return False

        if os.path.exists(output_path) and os.path.getsize(output_path) >= MIN_VIDEO_FILE_SIZE:
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            logger.info(f"ffmpeg HLS download success: {output_path} ({size_mb:.2f} MB)")
            return True
        else:
            file_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            logger.warning(f"ffmpeg output too small or missing ({file_size} bytes)")
            if os.path.exists(output_path):
                os.remove(output_path)
            return False

    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg timed out after 600s")
        if os.path.exists(output_path):
            os.remove(output_path)
        return False
    except Exception as e:
        logger.warning(f"ffmpeg failed: {e}", exc_info=True)
        if os.path.exists(output_path):
            os.remove(output_path)
        return False


def _extract_pornhub_video_urls(html: str) -> list:
    """
    Extract video URLs from PornHub's JavaScript flashvars/mediaDefinitions.
    PornHub embeds the actual video URLs in JS variables, not in plain HTML.
    Returns list of (quality, url) sorted by quality descending.
    """
    import re
    import json

    results = []

    # Pattern 1: mediaDefinitions in flashvars
    # Look for: "mediaDefinitions":[{...}]
    media_match = re.search(
        r'"mediaDefinitions"\s*:\s*(\[\{.*?\}\])',
        html, re.DOTALL
    )
    if media_match:
        try:
            definitions = json.loads(media_match.group(1))
            for item in definitions:
                video_url = item.get("videoUrl") or item.get("url") or ""
                quality = item.get("quality", "")
                format_type = item.get("format", "")

                # Skip empty URLs, HLS manifests (we want direct mp4)
                if not video_url:
                    continue
                # Prefer mp4 over HLS
                if format_type == "hls" or ".m3u8" in video_url:
                    # Keep HLS as fallback but mark it
                    results.append((f"hls-{quality}", video_url))
                else:
                    results.append((str(quality), video_url))

            logger.info(f"PornHub JS: found {len(results)} mediaDefinition URL(s)")
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug(f"Failed to parse mediaDefinitions: {e}")

    # Pattern 2: Direct quality URLs in flashvars
    # Look for: "quality_720p":"https://..."
    quality_matches = re.findall(
        r'"quality_(\d+)p"\s*:\s*"(https?://[^"]+)"',
        html
    )
    for quality, qurl in quality_matches:
        if qurl not in [u for _, u in results]:
            results.append((quality, qurl))

    # Pattern 3: data-mediabook or similar attributes with video URLs
    data_matches = re.findall(
        r'data-mediabook=["\']([^"\']*phncdn\.com[^"\']*\.mp4[^"\']*)["\']',
        html, re.IGNORECASE
    )
    for durl in data_matches:
        if durl not in [u for _, u in results]:
            results.append(("data-attr", durl))

    if results:
        # Sort: prefer mp4 over HLS, then by quality descending
        def sort_key(item):
            q, u = item
            is_hls = q.startswith("hls-")
            try:
                num = int(q.replace("hls-", ""))
            except ValueError:
                num = 0
            return (not is_hls, num)  # mp4 first, then highest quality

        results.sort(key=sort_key, reverse=True)
        logger.debug(f"PornHub JS sorted results: {[(q, u[:60]) for q, u in results]}")

    return results


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
                pass

    # ── Parse domain early (used by multiple branches) ───────────────────
    from urllib.parse import urlparse
    parsed = urlparse(url)
    domain = parsed.netloc.lower().replace("www.", "")
    logger.debug(f"Parsed domain: {domain}")

    # ── Terabox: hybrid proxy + native API ───────────────────────────────────
    if any(d in domain for d in TERABOX_DOMAINS):
        _update("📦 Terabox detected, extracting file info...")
        tb = TeraboxDownloader()
        info = tb.get_file_info(url)
        if info.get("status") != "success" or not info.get("list"):
            err = info.get("error", "Unknown error")
            raise RuntimeError(f"Failed to extract Terabox file info: {err}")

        file_list = info["list"]
        # Filter to video files; if none, use all files
        videos = [f for f in file_list if f.get("type") == "video"]
        targets = videos if videos else file_list

        total_count = len(targets)
        total_size = sum(f.get("size", 0) for f in targets) / (1024 * 1024)
        _update(f"📁 Found {total_count} file(s) ({total_size:.1f} MB total)")

        if total_count == 1:
            # Single file: download directly with progress updates
            fileinfo = targets[0]
            fname = fileinfo.get("filename", "unknown")
            size_mb = fileinfo.get("size", 0) / (1024 * 1024)
            _update(f"🔗 Downloading: {fname} ({size_mb:.1f} MB)")

            unique_prefix = uuid.uuid4().hex[:8]
            safe_name = re.sub(r'[<>:"/\\|?*]', '_', fname)
            output_path = os.path.join(download_dir, f"{unique_prefix}_{safe_name}")

            result = tb.download_file(
                fs_id=fileinfo["fs_id"],
                output_path=output_path,
                referer=url,
                max_retries=3,
                progress_callback=_update,
            )
            if result:
                _update(f"✅ Download complete! ({result['size_mb']:.1f} MB)")
                return result
            raise RuntimeError("Terabox download failed after all retries.")

        # Multiple files: download sequentially — Terabox CDN drops concurrent
        # connections from the same IP (IncompleteRead 0 bytes)
        downloaded = tb.download_files(
            files=targets,
            download_dir=download_dir,
            referer=url,
            max_workers=1,
            progress_callback=_update,
        )

        if not downloaded:
            raise RuntimeError("Terabox: failed to download any files.")

        if len(downloaded) == 1:
            return downloaded[0]

        # Multiple files: return the first one with a 'extra_files' key for the rest
        first = downloaded[0]
        first["extra_files"] = downloaded[1:]
        _update(f"✅ All done! Downloaded {len(downloaded)}/{total_count} files.")
        return first

    # ── Check if this is a CDN direct URL (phncdn.com, etc.) ─────────────
    is_adult_cdn = any(d in domain for d in ADULT_CDN_DOMAINS)

    if is_adult_cdn:
        referer = _get_cdn_referer(domain)
        _update("🎯 CDN video URL detected, downloading with proper headers...")
        unique_prefix = uuid.uuid4().hex[:8]
        filename = f"{unique_prefix}_{generate_filename(url)}"
        output_path = os.path.join(download_dir, filename)

        url_lower = url.lower()
        if ".m3u8" in url_lower:
            # HLS stream from CDN
            if not output_path.lower().endswith((".mp4", ".ts")):
                output_path = os.path.splitext(output_path)[0] + ".mp4"

            _update("⬇️ Downloading HLS stream...")
            # Try ffmpeg first (most reliable for HLS)
            success = _download_m3u8_ffmpeg(
                url, output_path, referer=referer, progress_callback=_update
            )
            # Fallback to native downloader
            if not success:
                import requests as req
                cdn_session = req.Session()
                cdn_session.headers.update({
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Referer": referer,
                })
                success = download_m3u8_native(
                    url, output_path, url, session=cdn_session, workers=workers
                )
            if success and os.path.exists(output_path):
                result = _validate_downloaded_file(output_path)
                _update(f"✅ Download complete! ({result['size_mb']:.1f} MB)")
                return result
            # Check .ts fallback
            ts_path = os.path.splitext(output_path)[0] + ".ts"
            if os.path.exists(ts_path):
                result = _validate_downloaded_file(ts_path)
                _update(f"✅ Download complete! ({result['size_mb']:.1f} MB)")
                return result
            raise RuntimeError(
                "HLS download failed. The stream URL may have expired — "
                "try sending the original page URL instead of the CDN link."
            )
        else:
            # Direct mp4/webm from CDN
            result = _download_direct_with_headers(
                url, output_path, referer=referer, progress_callback=_update
            )
            if result:
                _update(f"✅ Download complete! ({result['size_mb']:.1f} MB)")
                return result

            # If direct download failed (403/410), token is probably expired
            raise RuntimeError(
                "CDN download failed (token likely expired). "
                "Try sending the original page URL instead of the CDN link, "
                "e.g. https://www.pornhub.com/view_video.php?viewkey=..."
            )

    # ── Direct video URL fast-path (non-CDN) ─────────────────────────────
    url_lower = url.lower().split("?")[0]
    is_direct = any(url_lower.endswith(ext) for ext in DIRECT_VIDEO_PATTERNS)
    logger.debug(f"is_direct={is_direct}, url_lower={url_lower}")

    if is_direct:
        _update("🎯 Direct video URL detected, downloading...")
        unique_prefix = uuid.uuid4().hex[:8]
        filename = f"{unique_prefix}_{generate_filename(url)}"
        output_path = os.path.join(download_dir, filename)

        # Use proper headers even for generic direct URLs
        result = _download_direct_with_headers(
            url, output_path, referer=url, progress_callback=_update
        )
        if result:
            _update(f"✅ Download complete! ({result['size_mb']:.1f} MB)")
            return result

        # Fallback: try the original download_direct with a session
        import requests as req
        fallback_session = req.Session()
        fallback_session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        })

        if ".m3u8" in url.lower():
            if not output_path.lower().endswith((".mp4", ".ts")):
                output_path = os.path.splitext(output_path)[0] + ".mp4"
            _update("⬇️ Downloading HLS stream...")
            # Try ffmpeg first
            success = _download_m3u8_ffmpeg(
                url, output_path, referer=url, progress_callback=_update
            )
            # Fallback to native
            if not success:
                success = download_m3u8_native(
                    url, output_path, url, session=fallback_session, workers=workers
                )
        else:
            _update("⬇️ Downloading video...")
            download_direct(url, output_path, url, session=fallback_session)
            success = True

        if not success or not os.path.exists(output_path):
            ts_path = os.path.splitext(output_path)[0] + ".ts"
            if os.path.exists(ts_path):
                output_path = ts_path
            else:
                raise RuntimeError("Download completed but output file not found.")

        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        _update(f"✅ Download complete! ({size_mb:.1f} MB)")
        result = _validate_downloaded_file(output_path)
        return result

    # ── Adult sites: yt-dlp first (knows JS video extraction), scraper fallback ─
    if any(d in domain for d in ADULT_PAGE_DOMAINS):
        _update("🎬 Detected adult site, extracting video...")
        logger.info(f"Adult site detected ({domain}), trying yt-dlp (has native extractor)")
        unique_prefix = uuid.uuid4().hex[:8]
        output_path = os.path.join(download_dir, f"{unique_prefix}_video.mp4")
        try:
            result = _try_ytdlp(url, output_path, progress_callback=_update)
            if result:
                _update(f"✅ Download complete! ({result['size_mb']:.1f} MB)")
                return result
        except Exception as e:
            logger.warning(f"yt-dlp failed for adult site: {e}")
        _update("⚠️ yt-dlp failed, falling back to page scraping...")
        # Fall through to Step 1 (page scraping) below

    # ── Vidara.to / Vidara.so: call their stream API directly ────────────
    elif any(d in domain for d in VIDARA_DOMAINS):
        _update("🎬 Vidara detected, fetching stream...")
        unique_prefix = uuid.uuid4().hex[:8]
        output_path = os.path.join(download_dir, f"{unique_prefix}_vidara.mp4")
        result = _try_vidara(url, output_path, progress_callback=_update)
        if result:
            _update(f"✅ Download complete! ({result['size_mb']:.1f} MB)")
            return result
        _update("⚠️ Vidara API failed, falling back to page scraping...")

    # ── Twitter/X: use yt-dlp ────────────────────────────────────────────
    elif any(d in domain for d in TWITTER_DOMAINS):
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
        # ── Unknown site: try yt-dlp first (supports 1000+ sites) ────────
        _update("🎬 Trying yt-dlp...")
        logger.info(f"Unknown domain '{domain}', trying yt-dlp first")
        unique_prefix = uuid.uuid4().hex[:8]
        output_path = os.path.join(download_dir, f"{unique_prefix}_video.mp4")
        try:
            result = _try_ytdlp(url, output_path, progress_callback=_update)
            if result:
                _update(f"✅ Download complete! ({result['size_mb']:.1f} MB)")
                return result
        except Exception as e:
            logger.info(f"yt-dlp failed for {domain}: {e}")
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

    # For adult sites, try extracting from JavaScript first (most reliable)
    is_adult_page = any(d in domain for d in ADULT_PAGE_DOMAINS)
    if is_adult_page:
        js_urls = _extract_pornhub_video_urls(html)
        if js_urls:
            _update(f"🔎 Found {len(js_urls)} video URL(s) in page JavaScript")
            # Use the best JS-extracted URL directly
            best_quality, best_url = js_urls[0]
            logger.info(f"Using JS-extracted URL (quality={best_quality}): {best_url[:100]}")

            unique_prefix = uuid.uuid4().hex[:8]
            filename = f"{unique_prefix}_{generate_filename(best_url)}"
            output_path = os.path.join(download_dir, filename)

            # Determine referer from CDN domain
            cdn_domain = urlparse(best_url).netloc.lower()
            referer = _get_cdn_referer(cdn_domain) or url

            result = _download_direct_with_headers(
                best_url, output_path, referer=referer, progress_callback=_update
            )
            if result:
                _update(f"✅ Download complete! ({result['size_mb']:.1f} MB)")
                return _validate_downloaded_file(result["filepath"])

            # If best URL failed, try remaining JS URLs
            for quality, alt_url in js_urls[1:]:
                if alt_url == best_url:
                    continue
                logger.info(f"Trying fallback JS URL (quality={quality}): {alt_url[:80]}")
                alt_path = os.path.join(download_dir, f"{uuid.uuid4().hex[:8]}_{generate_filename(alt_url)}")
                alt_referer = _get_cdn_referer(urlparse(alt_url).netloc.lower()) or url
                result = _download_direct_with_headers(
                    alt_url, alt_path, referer=alt_referer, progress_callback=_update
                )
                if result:
                    _update(f"✅ Download complete! ({result['size_mb']:.1f} MB)")
                    return _validate_downloaded_file(result["filepath"])

            logger.warning("All JS-extracted URLs failed, falling back to generic extraction")

    video_urls = extract_video_urls(html, url)
    logger.debug(f"extract_video_urls returned {len(video_urls)} URL(s): {[(s, u[:80]) for s, u in video_urls]}")

    # Prepend network-captured URLs (from Playwright)
    # For adult sites, filter network URLs to avoid recommended video thumbnails
    for nurl in network_urls:
        if nurl not in [u for _, u in video_urls]:
            if is_adult_page and _is_ad_url(nurl):
                logger.debug(f"Skipping ad network URL: {nurl[:80]}")
                continue
            video_urls.insert(0, ("network-capture", nurl))
    logger.debug(f"After network-capture merge: {len(video_urls)} URL(s) total")

    # Filter out ad/tracking URLs
    video_urls = _filter_ad_urls(video_urls)

    downloadable = [(s, u) for s, u in video_urls if s != "iframe"]
    logger.debug(f"Downloadable (non-iframe): {len(downloadable)} — {[(s, u[:80]) for s, u in downloadable]}")

    # Try iframes if no direct URLs found
    if not downloadable:
        iframes = [(s, u) for s, u in video_urls if s == "iframe"]
        logger.debug(f"No direct URLs; checking {len(iframes)} iframe(s)")
        for _, iframe_url in iframes:
            # Skip ad iframes
            if _is_ad_url(iframe_url):
                logger.debug(f"Skipping ad iframe: {iframe_url[:80]}")
                continue
            logger.info(f"Checking iframe: {iframe_url[:80]}...")
            try:
                if session:
                    resp = session.get(iframe_url, timeout=15)
                    logger.debug(f"Iframe fetch status: {resp.status_code}")
                    if resp.status_code == 200:
                        extra = extract_video_urls(resp.text, iframe_url)
                        extra = _filter_ad_urls(extra)
                        logger.debug(f"Iframe yielded {len(extra)} extra URL(s)")
                        downloadable.extend(
                            [(s, u) for s, u in extra if s != "iframe"]
                        )
            except Exception as e:
                logger.warning(f"Iframe fetch failed: {e}")

    if not downloadable:
        # Last resort: try yt-dlp on the original URL (with ad filtering)
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
    # Priority order:
    # 1. m3u8 playlists (full HLS stream — always preferred over segments)
    # 2. mp4 direct URLs
    # 3. Network-captured URLs (real playback URLs, not ads)
    # 4. Other (js-pattern, html-tag, etc.)
    #
    # We specifically deprioritize individual .ts segment URLs because those
    # are only a single HLS segment (a few seconds), not the full video.
    def _url_priority(item):
        source, u = item
        u_lower = u.lower()
        # m3u8 playlists — best (gives us ALL segments)
        if ".m3u8" in u_lower:
            return 0
        # Individual .ts segments — worst (only a few seconds)
        if u_lower.split("?")[0].endswith(".ts"):
            return 4
        # Direct mp4 — great
        if ".mp4" in u_lower:
            return 1
        # Network-captured (non-ts, non-m3u8)
        if source == "network-capture":
            return 2
        # Everything else
        return 3

    downloadable.sort(key=_url_priority)

    _, chosen_url = downloadable[0]
    logger.info(f"Selected URL: {chosen_url[:100]}...")

    # ── Step 4: Generate unique filename ─────────────────────────────────
    unique_prefix = uuid.uuid4().hex[:8]
    filename = f"{unique_prefix}_{generate_filename(chosen_url)}"
    output_path = os.path.join(download_dir, filename)

    # ── Step 5: Download ─────────────────────────────────────────────────
    _update(f"⬇️ Downloading video...")

    # Determine the right referer for the download
    download_referer = url  # use the page URL as referer
    cdn_referer = _get_cdn_referer(
        urlparse(chosen_url).netloc.lower()
    )
    if cdn_referer:
        download_referer = cdn_referer

    if ".m3u8" in chosen_url.lower():
        if not output_path.lower().endswith((".mp4", ".ts")):
            output_path = os.path.splitext(output_path)[0] + ".mp4"

        # Try ffmpeg first (most reliable for HLS — downloads ALL segments)
        success = _download_m3u8_ffmpeg(
            chosen_url, output_path, referer=download_referer,
            progress_callback=_update
        )

        # Fallback to native downloader if ffmpeg unavailable or failed
        if not success:
            logger.info("ffmpeg HLS failed, trying native m3u8 downloader")
            if session is None:
                import requests as req
                session = req.Session()
            session.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": download_referer,
            })
            success = download_m3u8_native(
                chosen_url, output_path, url, session, workers=workers
            )
    else:
        # Try our improved direct downloader first
        result = _download_direct_with_headers(
            chosen_url, output_path, referer=download_referer,
            progress_callback=_update
        )
        if result:
            _update(f"✅ Download complete! ({result['size_mb']:.1f} MB)")
            return result

        # Fallback to original download_direct
        download_direct(chosen_url, output_path, url, session)
        success = True

    if not success or not os.path.exists(output_path):
        ts_path = os.path.splitext(output_path)[0] + ".ts"
        if os.path.exists(ts_path):
            output_path = ts_path
        else:
            # Last resort: try yt-dlp (with ad filtering)
            _update("⚠️ Scraper download failed, trying yt-dlp as last resort...")
            ytdlp_path = os.path.join(download_dir, f"{uuid.uuid4().hex[:8]}_video.mp4")
            result = _try_ytdlp(url, ytdlp_path, progress_callback=_update)
            if result:
                _update(f"✅ Download complete! ({result['size_mb']:.1f} MB)")
                return result
            raise RuntimeError("Download completed but output file not found.")

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    _update(f"✅ Download complete! ({size_mb:.1f} MB)")

    return _validate_downloaded_file(output_path)