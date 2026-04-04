#!/usr/bin/env python3
"""
Video Downloader with Cloudflare Bypass + HLS Decryption
=========================================================
Downloads videos from Cloudflare-protected pages.
Supports AES-128 encrypted HLS (.m3u8) streams — no ffmpeg required.

Install:
    pip install curl_cffi beautifulsoup4 tqdm pycryptodome

Optional (better mp4 muxing):
    Install ffmpeg from https://ffmpeg.org

Usage:
  python video_downloader.py <URL>
  python video_downloader.py <URL> -o my_video.mp4
  python video_downloader.py <URL> --list-only
  python video_downloader.py <URL> --method playwright
"""

import re
import os
import sys
import json
import time
import struct
import argparse
import urllib.parse
import subprocess
import tempfile
import shutil
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from bs4 import BeautifulSoup
    from tqdm import tqdm
except ImportError:
    print("Missing core dependencies. Run:")
    print("  pip install beautifulsoup4 tqdm")
    sys.exit(1)

# Check for crypto library
try:
    from Crypto.Cipher import AES
    HAS_CRYPTO = True
except ImportError:
    try:
        from Cryptodome.Cipher import AES
        HAS_CRYPTO = True
    except ImportError:
        HAS_CRYPTO = False


# ── Constants ────────────────────────────────────────────────────────────────

VIDEO_EXTENSIONS = (".mp4", ".m3u8", ".webm", ".mkv", ".avi", ".flv", ".ts", ".mov")

BROWSERS_TO_TRY = [
    "chrome120", "chrome119", "chrome116", "chrome110",
    "edge101", "safari15_5",
]

COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


# ── HTTP Helper ──────────────────────────────────────────────────────────────

def _make_headers(referer=None):
    headers = {**COMMON_HEADERS}
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = (
            urllib.parse.urlparse(referer).scheme + "://" +
            urllib.parse.urlparse(referer).netloc
        )
    return headers


def _is_http2_error(exc):
    """Check if an exception is an HTTP/2 protocol error."""
    msg = str(exc).lower()
    return any(k in msg for k in (
        "http/2 stream", "protocol_error", "curl: (92)",
        "nghttp2", "stream was not closed cleanly",
    ))


def http_get(url, referer=None, session=None, stream=False, timeout=30):
    """
    Unified GET that uses curl_cffi session if available, else plain requests.
    Automatically retries with HTTP/1.1 if HTTP/2 fails.
    Falls back to plain requests if curl_cffi keeps failing.
    """
    import requests as plain_requests
    headers = _make_headers(referer)

    if session and hasattr(session, "impersonate"):
        # ── Attempt 1: curl_cffi (default HTTP version) ──
        try:
            return session.get(url, headers=headers, timeout=timeout, stream=stream)
        except Exception as e:
            if not _is_http2_error(e):
                raise  # non-HTTP/2 error, don't mask it

            # ── Attempt 2: curl_cffi forced HTTP/1.1 ──
            print(f"  ⚠️  HTTP/2 error, retrying with HTTP/1.1...")
            try:
                from curl_cffi import CurlHttpVersion  # type: ignore
                return session.get(
                    url, headers=headers, timeout=timeout, stream=stream,
                    http_version=CurlHttpVersion.V1_1,
                )
            except Exception as e2:
                if not _is_http2_error(e2):
                    # It's a different error now — could be the http_version
                    # kwarg not supported in older curl_cffi. Try legacy way.
                    pass
                print(f"  ⚠️  curl_cffi still failing, falling back to plain requests...")

    # ── Attempt 3 (or primary if no session): plain requests ──
    return plain_requests.get(url, headers=headers, timeout=timeout, stream=stream)


# ── Page Fetchers ────────────────────────────────────────────────────────────

def fetch_with_curl_cffi(url):
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        print("  ⚠️  curl_cffi not installed. Run: pip install curl_cffi")
        return None, None

    for browser in BROWSERS_TO_TRY:
        try:
            print(f"  🌐 Trying curl_cffi with {browser}...")
            session = cffi_requests.Session(impersonate=browser)
            resp = session.get(url, timeout=30)
            if resp.status_code == 200 and len(resp.text) > 1000:
                print(f"  ✅ Success with {browser} (status {resp.status_code})")
                return resp.text, session
            else:
                print(f"  ❌ {browser}: status {resp.status_code}, body {len(resp.text)} bytes")
        except Exception as e:
            print(f"  ❌ {browser}: {e}")
    return None, None


def fetch_with_playwright(url):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ⚠️  Playwright not installed. Run:")
        print("     pip install playwright && playwright install chromium")
        return None, []

    print("  🌐 Launching headless Chromium...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",  # Disable sandbox (safe in Docker)
                "--disable-gpu",  # No GPU in container
                "--disable-dev-shm-usage",  # Use disk instead of /dev/shm for large pages
            ],
        )
        context = browser.new_context(
            user_agent=COMMON_HEADERS["User-Agent"],
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()
        video_network_urls = []

        def on_response(response):
            u = response.url
            ct = response.headers.get("content-type", "")
            status = response.status

            # Capture actual video content responses (the real CDN URLs)
            if "video" in ct or "octet-stream" in ct:
                # For redirected responses, the final URL is what we want
                video_network_urls.append(u)
                print(f"    📡 Captured (content-type: {ct}): {u[:80]}...")

            # Also capture by URL extension
            elif any(ext in u.lower().split("?")[0] for ext in VIDEO_EXTENSIONS):
                video_network_urls.append(u)
                print(f"    📡 Captured (extension match): {u[:80]}...")

            # Capture redirects (301/302/307/308) pointing to video
            if status in (301, 302, 307, 308):
                location = response.headers.get("location", "")
                if location and _looks_like_video(location):
                    resolved = _resolve(location, u)
                    video_network_urls.append(resolved)
                    print(f"    📡 Captured (redirect → video): {resolved[:80]}...")

        def on_request(request):
            u = request.url
            # Capture requests with video resource type
            if request.resource_type in ("media", "video"):
                if u not in video_network_urls:
                    video_network_urls.append(u)
                    print(f"    📡 Captured (media request): {u[:80]}...")

        page.on("response", on_response)
        page.on("request", on_request)
        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
            time.sleep(2)

            # Try multiple selectors to trigger video playback
            play_selectors = [
                "video",
                ".play-btn", ".vjs-big-play-button",
                "[class*=play]", "[id*=play]",
                "button[class*=play]", ".video-play",
                ".player-btn", "[aria-label*=play]",
            ]
            for selector in play_selectors:
                try:
                    page.click(selector, timeout=1500)
                    print(f"    ▶️  Clicked: {selector}")
                    time.sleep(2)
                    break
                except Exception:
                    continue

            # Wait for video to actually start loading
            time.sleep(5)

            # Try to extract currentSrc from video elements (the resolved URL)
            try:
                js_urls = page.evaluate("""
                    () => {
                        const urls = [];
                        document.querySelectorAll('video').forEach(v => {
                            if (v.currentSrc) urls.push(v.currentSrc);
                            if (v.src) urls.push(v.src);
                        });
                        // Check for common player APIs
                        if (window.player && window.player.src) urls.push(
                            typeof window.player.src === 'function'
                                ? window.player.src() : window.player.src
                        );
                        if (window.videoUrl) urls.push(window.videoUrl);
                        if (window.video_url) urls.push(window.video_url);
                        return urls;
                    }
                """)
                for js_url in js_urls:
                    if js_url and js_url not in video_network_urls:
                        video_network_urls.append(js_url)
                        print(f"    📡 Captured (JS runtime): {js_url[:80]}...")
            except Exception:
                pass

            html = page.content()
            # Deduplicate while preserving order
            seen = set()
            deduped = []
            for u in video_network_urls:
                if u not in seen:
                    seen.add(u)
                    deduped.append(u)
            video_network_urls = deduped

            print(f"  ✅ Page loaded ({len(html)} bytes), captured {len(video_network_urls)} video URL(s)")
            browser.close()
            return html, video_network_urls
        except Exception as e:
            print(f"  ❌ Playwright error: {e}")
            browser.close()
            return None, []


# ── Video URL Extraction ─────────────────────────────────────────────────────

def extract_video_urls(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    urls = []

    for tag in soup.find_all(["video", "source"]):
        for attr in ("src", "data-src", "data-url", "data-video"):
            val = tag.get(attr)
            if val:
                urls.append(("html-tag", _resolve(val, base_url)))

    js_patterns = [
        r'''["'](https?://[^"'\s]+\.(?:mp4|m3u8|webm|flv|ts)(?:\?[^"'\s]*)?)["']''',
        r'''(?:source|src|url|file|video_url|videoUrl|video_src|playUrl|play_url|videoSrc)\s*[:=]\s*["'](https?://[^"'\s]+)["']''',
        r'''"(?:url|src|file|source|video|mp4|hls|stream)"\s*:\s*"(https?://[^"]+)"''',
        r'''video\.src\s*=\s*["'](https?://[^"']+)["']''',
        r'''(?:loadVideo|playVideo|initPlayer|setSource)\s*\(\s*["'](https?://[^"']+)["']''',
        r'''(https?:\\?/\\?/[^"'\s\\]+\.(?:mp4|m3u8)[^"'\s\\]*)''',
    ]
    for pattern in js_patterns:
        for match in re.finditer(pattern, html, re.IGNORECASE):
            raw = match.group(1).replace("\\/", "/")
            if _looks_like_video(raw):
                urls.append(("js-pattern", _resolve(raw, base_url)))

    for meta in soup.find_all("meta"):
        prop = meta.get("property", "") or meta.get("name", "")
        content = meta.get("content", "")
        if any(k in prop.lower() for k in ("video", "player")) and content:
            if _looks_like_video(content):
                urls.append(("meta-tag", _resolve(content, base_url)))

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            _walk_json(data, urls, base_url)
        except (json.JSONDecodeError, TypeError):
            pass

    for iframe in soup.find_all("iframe"):
        src = iframe.get("src") or iframe.get("data-src")
        if src:
            urls.append(("iframe", _resolve(src, base_url)))

    return _prioritize(_deduplicate(urls))


def _walk_json(obj, urls, base_url):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and _looks_like_video(v):
                urls.append(("json-ld", _resolve(v, base_url)))
            else:
                _walk_json(v, urls, base_url)
    elif isinstance(obj, list):
        for item in obj:
            _walk_json(item, urls, base_url)


def _resolve(url, base_url):
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        p = urllib.parse.urlparse(base_url)
        return f"{p.scheme}://{p.netloc}{url}"
    if not url.startswith("http"):
        return urllib.parse.urljoin(base_url, url)
    return url


def _looks_like_video(url):
    lower = url.lower().split("?")[0]
    if any(lower.endswith(ext) for ext in VIDEO_EXTENSIONS):
        return True
    keywords = ["video", "media", "stream", "play", "hls", "dash", ".mp4", ".m3u8"]
    return any(kw in url.lower() for kw in keywords)


def _deduplicate(video_urls):
    seen = set()
    unique = []
    for source, url in video_urls:
        clean = url.split("?")[0]
        if clean not in seen:
            seen.add(clean)
            unique.append((source, url))
    return unique


def _prioritize(video_urls):
    def key(item):
        url = item[1].lower()
        if ".mp4" in url:
            return 0
        if ".m3u8" in url:
            return 1
        return 2
    return sorted(video_urls, key=key)


# ── HLS Parser ───────────────────────────────────────────────────────────────

class HLSSegment:
    """Represents a single HLS segment with optional encryption info."""
    def __init__(self, url, index, key_url=None, iv=None, key_method=None):
        self.url = url
        self.index = index
        self.key_url = key_url
        self.iv = iv
        self.key_method = key_method  # "AES-128" or "NONE" or None


def parse_m3u8_playlist(content, base_url):
    """
    Parse m3u8 playlist, extracting segments with encryption info.
    Returns (segments, is_master_playlist).
    """
    lines = content.strip().splitlines()
    segments = []
    is_master = False

    current_key_method = None
    current_key_url = None
    current_iv = None
    seg_index = 0

    for i, line in enumerate(lines):
        line = line.strip()

        # Master playlist indicator
        if line.startswith("#EXT-X-STREAM-INF"):
            is_master = True

        # Encryption key
        elif line.startswith("#EXT-X-KEY"):
            key_attrs = _parse_m3u8_attributes(line)
            current_key_method = key_attrs.get("METHOD", "NONE")

            if current_key_method == "AES-128":
                uri = key_attrs.get("URI", "")
                current_key_url = _resolve(uri, base_url)
                iv_hex = key_attrs.get("IV")
                if iv_hex:
                    # IV is specified as 0x... hex string
                    current_iv = bytes.fromhex(iv_hex.replace("0x", "").replace("0X", ""))
                else:
                    current_iv = None  # Will use segment index as IV
            elif current_key_method == "NONE":
                current_key_url = None
                current_iv = None

        # Segment URL
        elif not line.startswith("#") and line:
            seg_url = _resolve(line, base_url)
            seg = HLSSegment(
                url=seg_url,
                index=seg_index,
                key_url=current_key_url,
                iv=current_iv,
                key_method=current_key_method,
            )
            segments.append(seg)
            seg_index += 1

    return segments, is_master


def _parse_m3u8_attributes(line):
    """Parse attributes from an HLS tag like #EXT-X-KEY:METHOD=AES-128,URI="..."."""
    attrs = {}
    # Get everything after the colon
    match = re.match(r'#[^:]+:(.*)', line)
    if not match:
        return attrs

    attr_string = match.group(1)

    # Parse key=value pairs (values may be quoted)
    for m in re.finditer(r'([A-Z0-9-]+)=("([^"]*)"|([\w./-]+))', attr_string):
        key = m.group(1)
        value = m.group(3) if m.group(3) is not None else m.group(4)
        attrs[key] = value

    return attrs


def select_best_stream(master_url, referer, session):
    """From a master playlist, pick the highest quality stream."""
    print("  📋 Fetching master playlist...")
    resp = http_get(master_url, referer=referer, session=session)
    resp.raise_for_status()
    content = resp.text
    lines = content.strip().splitlines()

    streams = []
    current_bandwidth = 0
    current_resolution = ""

    for line in lines:
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            bw_match = re.search(r'BANDWIDTH=(\d+)', line)
            res_match = re.search(r'RESOLUTION=(\S+)', line)
            current_bandwidth = int(bw_match.group(1)) if bw_match else 0
            current_resolution = res_match.group(1) if res_match else "unknown"
        elif not line.startswith("#") and line:
            stream_url = _resolve(line, master_url)
            streams.append({
                "url": stream_url,
                "bandwidth": current_bandwidth,
                "resolution": current_resolution,
            })

    if not streams:
        return master_url

    streams.sort(key=lambda s: s["bandwidth"], reverse=True)

    print(f"\n  Available qualities:")
    for i, s in enumerate(streams):
        bw_mbps = s["bandwidth"] / 1_000_000
        marker = " ◀ best" if i == 0 else ""
        print(f"    [{i+1}] {s['resolution']}  ({bw_mbps:.1f} Mbps){marker}")

    best = streams[0]
    print(f"\n  ✅ Selected: {best['resolution']} ({best['bandwidth']/1_000_000:.1f} Mbps)")
    return best["url"]


# ── AES-128 Decryption ───────────────────────────────────────────────────────

class HLSDecryptor:
    """Handles fetching encryption keys and decrypting HLS segments."""

    def __init__(self, referer, session):
        self.referer = referer
        self.session = session
        self._key_cache = {}  # cache fetched keys by URL

    def get_key(self, key_url):
        """Fetch and cache an encryption key."""
        if key_url in self._key_cache:
            return self._key_cache[key_url]

        resp = http_get(key_url, referer=self.referer, session=self.session)
        resp.raise_for_status()
        key = resp.content
        if len(key) != 16:
            raise ValueError(f"Invalid AES-128 key length: {len(key)} (expected 16)")

        self._key_cache[key_url] = key
        return key

    def decrypt_segment(self, data, segment):
        """Decrypt a segment's data using its encryption info."""
        if not segment.key_method or segment.key_method == "NONE":
            return data  # No encryption

        if segment.key_method != "AES-128":
            print(f"  ⚠️  Unsupported encryption: {segment.key_method}")
            return data

        if not HAS_CRYPTO:
            raise RuntimeError(
                "Encrypted HLS stream! Install pycryptodome:\n"
                "  pip install pycryptodome"
            )

        key = self.get_key(segment.key_url)

        # IV: use explicit IV or derive from segment index
        if segment.iv:
            iv = segment.iv
        else:
            iv = struct.pack(">QQ", 0, segment.index)  # 16-byte IV from index

        # Ensure IV is exactly 16 bytes
        if len(iv) < 16:
            iv = b'\x00' * (16 - len(iv)) + iv
        elif len(iv) > 16:
            iv = iv[:16]

        cipher = AES.new(key, AES.MODE_CBC, iv=iv)
        decrypted = cipher.decrypt(data)

        # Remove PKCS7 padding
        if decrypted:
            pad_len = decrypted[-1]
            if 0 < pad_len <= 16 and decrypted[-pad_len:] == bytes([pad_len]) * pad_len:
                decrypted = decrypted[:-pad_len]

        return decrypted


# ── HLS Downloader ───────────────────────────────────────────────────────────

def download_m3u8_native(m3u8_url, output_path, referer, session=None, workers=8):
    """Download HLS stream: fetch segments, decrypt if needed, merge."""

    # Step 1: Fetch the m3u8 playlist
    print(f"\n📋 Fetching playlist: {m3u8_url[:80]}...")
    resp = http_get(m3u8_url, referer=referer, session=session)
    resp.raise_for_status()
    playlist_content = resp.text

    segments, is_master = parse_m3u8_playlist(playlist_content, m3u8_url)

    # If master playlist, select best quality and re-fetch
    if is_master:
        actual_m3u8 = select_best_stream(m3u8_url, referer, session)
        resp = http_get(actual_m3u8, referer=referer, session=session)
        resp.raise_for_status()
        playlist_content = resp.text
        segments, _ = parse_m3u8_playlist(playlist_content, actual_m3u8)

    # Filter to actual media segments (not nested playlists)
    segments = [s for s in segments if not s.url.endswith(".m3u8")]

    if not segments:
        print("  ❌ No segments found in playlist!")
        return False

    # Check encryption
    encrypted_count = sum(1 for s in segments if s.key_method == "AES-128")
    if encrypted_count > 0:
        print(f"  🔐 Stream is AES-128 encrypted ({encrypted_count} encrypted segments)")
        if not HAS_CRYPTO:
            print("\n  ❌ pycryptodome is required to decrypt this stream!")
            print("     Install it with: pip install pycryptodome")
            print(f"\n  Alternatively, use ffmpeg directly:")
            print(f'     ffmpeg -i "{m3u8_url}" -c copy "{output_path}"')
            return False
    else:
        print(f"  🔓 Stream is not encrypted")

    print(f"  📦 Found {len(segments)} segments to download")

    # Step 2: Set up decryptor
    decryptor = HLSDecryptor(referer=referer, session=session)

    # Pre-fetch encryption keys
    key_urls = set(s.key_url for s in segments if s.key_url)
    if key_urls:
        print(f"  🔑 Fetching {len(key_urls)} encryption key(s)...")
        for key_url in key_urls:
            try:
                decryptor.get_key(key_url)
                print(f"     ✅ Key fetched successfully")
            except Exception as e:
                print(f"     ❌ Failed to fetch key: {e}")
                return False

    # Step 3: Download & decrypt segments
    tmp_dir = tempfile.mkdtemp(prefix="hls_download_")

    try:
        segment_files = [None] * len(segments)
        failed = []

        def download_and_decrypt(seg):
            seg_path = os.path.join(tmp_dir, f"segment_{seg.index:05d}.ts")
            max_retries = 3

            for attempt in range(max_retries):
                try:
                    r = http_get(seg.url, referer=referer, session=session)
                    r.raise_for_status()
                    data = r.content

                    # Decrypt if needed
                    data = decryptor.decrypt_segment(data, seg)

                    with open(seg_path, "wb") as f:
                        f.write(data)

                    return seg.index, seg_path, len(data), None

                except Exception as e:
                    if attempt == max_retries - 1:
                        return seg.index, None, 0, str(e)
                    time.sleep(1 * (attempt + 1))

        print(f"  ⬇️  Downloading segments ({workers} threads)...\n")

        total_bytes = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(download_and_decrypt, seg): seg.index for seg in segments}

            with tqdm(total=len(segments), unit="seg", desc="  Progress") as pbar:
                for future in as_completed(futures):
                    idx, seg_path, size, error = future.result()
                    if error:
                        failed.append((idx, error))
                    else:
                        segment_files[idx] = seg_path
                        total_bytes += size
                    pbar.update(1)

        if failed:
            print(f"\n  ⚠️  {len(failed)} segment(s) failed:")
            for idx, err in failed[:5]:
                print(f"      Segment {idx}: {err}")
            if len(failed) > 5:
                print(f"      ... and {len(failed) - 5} more")

        # Step 4: Merge segments
        valid_segments = [f for f in segment_files if f is not None]

        if not valid_segments:
            print("\n  ❌ No segments downloaded successfully!")
            return False

        success_rate = len(valid_segments) / len(segments) * 100
        print(f"\n  📊 Downloaded {len(valid_segments)}/{len(segments)} segments ({success_rate:.0f}%)")
        print(f"  📦 Total data: {total_bytes / (1024*1024):.1f} MB")
        print(f"\n  🔗 Merging segments...")

        if _has_ffmpeg():
            return _merge_with_ffmpeg(valid_segments, output_path, tmp_dir)
        else:
            return _merge_direct(valid_segments, output_path)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _has_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _merge_with_ffmpeg(segment_files, output_path, tmp_dir):
    """Merge with ffmpeg → proper .mp4."""
    if not output_path.lower().endswith(".mp4"):
        output_path = os.path.splitext(output_path)[0] + ".mp4"

    concat_path = os.path.join(tmp_dir, "concat.txt")
    with open(concat_path, "w") as f:
        for seg in segment_files:
            # Escape single quotes in path
            safe_path = seg.replace("'", "'\\''")
            f.write(f"file '{safe_path}'\n")

    print(f"  🎬 Muxing to MP4 with ffmpeg...")

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-f", "concat", "-safe", "0",
        "-i", concat_path,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"\n  ✅ Download complete! ({size_mb:.1f} MB) → {output_path}")
        return True
    else:
        print(f"  ⚠️  ffmpeg muxing error: {result.stderr[:200]}")
        print(f"  Falling back to direct merge...")
        return _merge_direct(segment_files, output_path)


def _merge_direct(segment_files, output_path):
    """Merge by binary concatenation → .ts file (playable in VLC/mpv)."""
    ts_path = os.path.splitext(output_path)[0] + ".ts"

    with open(ts_path, "wb") as outf:
        for seg_path in tqdm(segment_files, desc="  Merging", unit="seg"):
            with open(seg_path, "rb") as inf:
                shutil.copyfileobj(inf, outf)

    size_mb = os.path.getsize(ts_path) / (1024 * 1024)
    print(f"\n  ✅ Download complete! ({size_mb:.1f} MB) → {ts_path}")
    print(f"\n  ▶️  Play with: vlc \"{ts_path}\"")
    print(f"                mpv \"{ts_path}\"")
    print(f"\n  💡 To convert to .mp4, install ffmpeg:")
    print(f'     ffmpeg -i "{ts_path}" -c copy "{output_path}"')
    return True


# ── URL Resolution ────────────────────────────────────────────────────────────

def resolve_video_url(url, referer, session=None):
    """
    Follow redirects via HEAD/GET to find the final CDN URL.
    Many video sites return a page-level URL that 302-redirects to the real file.
    """
    import requests as plain_requests
    headers = _make_headers(referer)

    # Try HEAD first (lighter), then GET with stream=True
    for method_label, do_request in [
        ("HEAD (curl_cffi)", lambda: session.head(url, headers=headers, timeout=15, allow_redirects=True) if session and hasattr(session, "head") else None),
        ("HEAD (requests)", lambda: plain_requests.head(url, headers=headers, timeout=15, allow_redirects=True)),
        ("GET (requests)", lambda: plain_requests.get(url, headers=headers, timeout=15, stream=True, allow_redirects=True)),
    ]:
        try:
            resp = do_request()
            if resp is None:
                continue
            final_url = resp.url if hasattr(resp, "url") else url
            if final_url != url:
                print(f"  🔀 URL resolved via {method_label}:")
                print(f"     From: {url[:80]}...")
                print(f"     To:   {final_url[:80]}...")
                return final_url
            # Even if URL didn't change, check if server responds OK
            if resp.status_code == 200:
                return url  # URL is valid as-is
        except Exception:
            continue

    return url  # Return original if resolution fails


# ── Direct File Download ─────────────────────────────────────────────────────

def download_direct(url, output_path, referer, session=None):
    """
    Download a direct video file (mp4, webm, etc.).
    Resolves redirects first, then retries with HTTP/1.1 and plain requests.
    """
    import requests as plain_requests

    # Step 0: Try to resolve the real URL (follow redirects)
    print(f"\n🔗 Resolving video URL...")
    resolved_url = resolve_video_url(url, referer, session)

    print(f"\n⬇️  Downloading: {resolved_url[:100]}...")
    print(f"   Saving to:   {output_path}\n")

    headers = _make_headers(referer)

    # Build download strategies — try resolved URL first, then original
    urls_to_try = [resolved_url]
    if resolved_url != url:
        urls_to_try.append(url)  # fallback to original if resolved fails

    for attempt_url in urls_to_try:
        strategies = []

        if session and hasattr(session, "impersonate"):
            # Strategy 1: curl_cffi forced HTTP/1.1 (try this first since HTTP/2 already failed)
            def _curl_h1(u=attempt_url):
                try:
                    from curl_cffi import CurlHttpVersion  # type: ignore
                    return session.get(
                        u, headers=headers, timeout=120, stream=True,
                        http_version=CurlHttpVersion.V1_1,
                    )
                except TypeError:
                    raise
            strategies.append(("curl_cffi (HTTP/1.1)", _curl_h1))

            # Strategy 2: curl_cffi default
            strategies.append(("curl_cffi (default)", lambda u=attempt_url: session.get(
                u, headers=headers, timeout=120, stream=True,
            )))

        # Strategy 3: plain requests — try without Referer too (some CDNs reject cross-origin)
        strategies.append(("plain requests", lambda u=attempt_url: plain_requests.get(
            u, headers=headers, timeout=120, stream=True,
        )))

        bare_headers = {**COMMON_HEADERS}  # no Referer/Origin
        strategies.append(("plain requests (no referer)", lambda u=attempt_url: plain_requests.get(
            u, headers=bare_headers, timeout=120, stream=True,
        )))

        resp = None
        for label, fetch_fn in strategies:
            try:
                url_label = attempt_url[:60]
                print(f"  🔄 Trying {label} → {url_label}...")
                resp = fetch_fn()
                resp.raise_for_status()
                print(f"  ✅ Connected via {label}")
                break
            except Exception as e:
                print(f"  ⚠️  {label} failed: {e}")
                resp = None
                continue

        if resp is not None:
            break

    if resp is None:
        raise RuntimeError("All download strategies failed. See errors above.")

    total = int(resp.headers.get("content-length", 0))

    with open(output_path, "wb") as f:
        with tqdm(total=total or None, unit="B", unit_scale=True, unit_divisor=1024) as pbar:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    pbar.update(len(chunk))

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"\n✅ Download complete! ({size_mb:.1f} MB) → {output_path}")


# ── Main ─────────────────────────────────────────────────────────────────────

def generate_filename(url, custom_name=None):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    def add_timestamp(name):
        if "." in name:
            base, ext = name.rsplit(".", 1)
            return f"{base}_{timestamp}.{ext}"
        return f"{name}_{timestamp}"
    if custom_name:
        if not any(custom_name.endswith(ext) for ext in VIDEO_EXTENSIONS):
            custom_name += ".mp4"
        return custom_name
    parsed = urllib.parse.urlparse(url)
    filename = os.path.basename(parsed.path)
    if not filename or not any(filename.endswith(ext) for ext in VIDEO_EXTENSIONS):
        filename = "video_download.mp4"
    return re.sub(r'[<>:"/\\|?*]', "_", add_timestamp(filename))


def main():
    parser = argparse.ArgumentParser(
        description="Video Downloader with Cloudflare bypass + HLS decryption"
    )
    parser.add_argument("url", help="URL of the page containing the video")
    parser.add_argument("-o", "--output", help="Output filename")
    parser.add_argument("-d", "--dir", default=".", help="Output directory")
    parser.add_argument("--direct-url", help="Direct video URL (skip page extraction, e.g. from IDM/DevTools)")
    parser.add_argument("--list-only", action="store_true", help="Only list found URLs")
    parser.add_argument("--method", choices=["auto", "curl_cffi", "playwright"],
                        default="auto", help="Fetch method (default: auto)")
    parser.add_argument("--threads", type=int, default=8,
                        help="Download threads for HLS (default: 8)")
    args = parser.parse_args()

    page_url = args.url
    html = None
    cffi_session = None
    network_urls = []

    print(f"\n{'═' * 60}")
    print(f"  Video Downloader")
    print(f"  Cloudflare Bypass + HLS Decryption")
    print(f"{'═' * 60}")
    print(f"\n📄 Target: {page_url}")

    # ── Direct URL mode (skip extraction) ────────────────────────────────
    if args.direct_url:
        print(f"\n🎯 Direct URL mode (skipping page extraction)")
        chosen_url = args.direct_url
        filename = generate_filename(chosen_url, args.output)
        output_path = os.path.join(args.dir, filename)
        os.makedirs(args.dir, exist_ok=True)
        try:
            if ".m3u8" in chosen_url.lower():
                if not output_path.lower().endswith((".mp4", ".ts")):
                    output_path = os.path.splitext(output_path)[0] + ".mp4"
                download_m3u8_native(chosen_url, output_path, page_url, None, args.threads)
            else:
                download_direct(chosen_url, output_path, page_url, None)
        except KeyboardInterrupt:
            print("\n\n⛔ Download cancelled.")
            sys.exit(0)
        except Exception as e:
            print(f"\n❌ Download failed: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)
        return

    # ── Step 1: Fetch the page ───────────────────────────────────────────
    if args.method in ("auto", "curl_cffi"):
        print("\n[Method 1] curl_cffi (browser TLS impersonation)...")
        html, cffi_session = fetch_with_curl_cffi(page_url)

    if html is None and args.method in ("auto", "playwright"):
        print("\n[Method 2] Playwright (headless browser)...")
        result = fetch_with_playwright(page_url)
        if result:
            html, network_urls = result

    if html is None:
        print("\n❌ Could not fetch the page. Install one of:")
        print("   pip install curl_cffi")
        print("   pip install playwright && playwright install chromium")
        sys.exit(1)

    # ── Step 2: Extract video URLs ───────────────────────────────────────
    print(f"\n🔎 Scanning page for video URLs...\n")
    video_urls = extract_video_urls(html, page_url)

    for nurl in network_urls:
        if nurl not in [u for _, u in video_urls]:
            video_urls.insert(0, ("network-capture", nurl))

    downloadable = [(s, u) for s, u in video_urls if s != "iframe"]

    if not downloadable:
        iframes = [(s, u) for s, u in video_urls if s == "iframe"]
        for _, iframe_url in iframes:
            print(f"\n🔍 Checking iframe: {iframe_url[:80]}...")
            try:
                if cffi_session:
                    resp = cffi_session.get(iframe_url, timeout=15)
                    if resp.status_code == 200:
                        extra = extract_video_urls(resp.text, iframe_url)
                        downloadable.extend([(s, u) for s, u in extra if s != "iframe"])
            except Exception as e:
                print(f"  ⚠️  {e}")

    if not downloadable:
        print("\n❌ No downloadable video URLs found.")
        print("  • Try: --method playwright")
        sys.exit(1)

    # ── Step 3: Display results ──────────────────────────────────────────
    # Reorder: network-captured first (these are the real playback URLs)
    network_items = [(s, u) for s, u in downloadable if s == "network-capture"]
    other_items = [(s, u) for s, u in downloadable if s != "network-capture"]
    downloadable = network_items + other_items

    print(f"\n{'─' * 60}")
    print(f"  Found {len(downloadable)} video URL(s):\n")
    for i, (source, url) in enumerate(downloadable, 1):
        label = url[:80] + ("..." if len(url) > 80 else "")
        rec = " ◀ RECOMMENDED" if source == "network-capture" else ""
        print(f"  [{i}] ({source}) {label}{rec}")
    print(f"{'─' * 60}")

    if args.list_only:
        print("\nFull URLs:")
        for _, url in downloadable:
            print(f"  {url}")
        return

    # ── Step 4: Choose and download ──────────────────────────────────────
    if len(downloadable) == 1:
        choice = 0
    else:
        print(f"\nWhich video? [1-{len(downloadable)}, default=1]: ", end="")
        try:
            inp = input().strip()
            choice = int(inp) - 1 if inp else 0
        except (ValueError, EOFError):
            choice = 0

    _, chosen_url = downloadable[choice]
    filename = generate_filename(chosen_url, args.output)
    output_path = os.path.join(args.dir, filename)
    os.makedirs(args.dir, exist_ok=True)

    try:
        if ".m3u8" in chosen_url.lower():
            if not output_path.lower().endswith((".mp4", ".ts")):
                output_path = os.path.splitext(output_path)[0] + ".mp4"
            download_m3u8_native(
                chosen_url, output_path, page_url, cffi_session, args.threads
            )
        else:
            download_direct(chosen_url, output_path, page_url, cffi_session)
    except KeyboardInterrupt:
        print("\n\n⛔ Download cancelled.")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Download failed: {e}")
        import traceback
        traceback.print_exc()
        print(f"\n💡 Tips:")
        print(f"  1. Try with Playwright to capture the real URL:")
        print(f'     python {sys.argv[0]} "{page_url}" --method playwright')
        print(f"  2. Copy the video URL from IDM/DevTools and use --direct-url:")
        print(f'     python {sys.argv[0]} "{page_url}" --direct-url "PASTE_URL_HERE"')
        print(f"  3. Manual curl fallback:")
        print(f'     curl -L -o "{filename}" -H "Referer: {page_url}" "{chosen_url}"')
        sys.exit(1)


if __name__ == "__main__":
    main()