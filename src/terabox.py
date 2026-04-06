"""
Terabox download module.

Uses a hybrid approach:
1. Extract surl from URL (query param or redirect)
2. Use terabox.hnn.workers.dev proxy to get file info, sign, timestamp
3. Navigate subdirectories to find downloadable files
4. Generate download link via hnn workers proxy
5. Fallback: use native Terabox API with cookies for file listing

Based on: https://github.com/Dapunta/TeraDL
"""
import re
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urlparse, parse_qs

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

TERABOX_DOMAINS = (
    "terabox.com", "1024terabox.com", "freeterabox.com", "nephobox.com",
    "terabox.app", "teraboxapp.com", "4funbox.com", "mirrobox.com",
    "momerybox.com", "teraboxlink.com",
)

HNN_API_BASE = "https://terabox.hnn.workers.dev"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36"
)

# Headers that match what the hnn workers.dev proxy expects (Chrome 143 fingerprint)
# Note: requests needs 'brotli' package for br — stick to gzip, deflate only
HNN_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "sec-ch-ua": '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Priority": "u=1, i",
    "Referer": f"{HNN_API_BASE}/",
    "Origin": HNN_API_BASE,
    "User-Agent": UA,
}


def _extract_surl(url: str) -> str | None:
    """Extract the short URL code from a Terabox sharing link."""
    # Try query param first: ?surl=XXX
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "surl" in qs:
        return qs["surl"][0]

    # Try /s/1XXX path format
    m = re.search(r"/s/1([A-Za-z0-9_-]+)", url)
    if m:
        return m.group(1)

    # Try following redirect
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, allow_redirects=True, timeout=15)
        m = re.search(r"surl=([^&]+)", resp.url)
        if m:
            return m.group(1)
    except Exception as e:
        logger.warning(f"Terabox: redirect failed: {e}")

    return None


def _extract_path(url: str) -> str | None:
    """Extract the path query parameter from a Terabox sharing link."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "path" in qs:
        return qs["path"][0]
    return None


def _get_base_url(url: str) -> str:
    """Get the base URL (scheme + host) from a Terabox URL."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _check_file_type(name: str) -> str:
    """Determine file type from filename."""
    name = name.lower()
    if any(ext in name for ext in [".mp4", ".mov", ".m4v", ".mkv", ".asf", ".avi", ".wmv", ".m2ts", ".3g2", ".webm"]):
        return "video"
    elif any(ext in name for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"]):
        return "image"
    elif any(ext in name for ext in [".pdf", ".docx", ".zip", ".rar", ".7z"]):
        return "file"
    elif any(ext in name for ext in [".mp3", ".aac", ".flac", ".ogg", ".wav"]):
        return "audio"
    else:
        return "other"


class TeraboxDownloader:
    """
    Downloads files from Terabox share links.

    Flow:
    1. Extract surl from URL
    2. Use hnn workers proxy to get file info, sign, timestamp
    3. Navigate subdirectories if needed (hnn proxy or native API)
    4. Generate download link via hnn workers proxy
    """

    def __init__(self):
        # API session — plain requests with Chrome 143 headers (no cloudscraper needed,
        # the hnn workers.dev proxy handles Cloudflare on the Terabox side)
        self.sc = requests.Session()
        self.sc.headers.update(HNN_HEADERS)
        adapter = HTTPAdapter(pool_connections=2, pool_maxsize=2)
        self.sc.mount("https://", adapter)
        self.sc.mount("http://", adapter)

        self.native_session = requests.Session()
        self.native_session.headers.update({"User-Agent": UA})
        # Separate plain requests session for large file downloads.
        # cloudscraper buffers full responses in memory for challenge
        # detection, which causes OOM on large files. CDN download URLs
        # (e.g. dm-d.1024tera.com) don't need Cloudflare bypass.
        self.dl_session = requests.Session()
        self.dl_session.headers.update({"User-Agent": UA})
        # Connection pool: reuse TCP connections across multiple file downloads
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4)
        self.dl_session.mount("https://", adapter)
        self.dl_session.mount("http://", adapter)
        self.sign = ""
        self.timestamp = ""
        self.shareid = ""
        self.uk = ""
        self.randsk = ""
        self.surl = ""
        self.base_url = ""

    def get_file_info(self, url: str) -> dict:
        """
        Get file info from a Terabox share URL.

        If the URL contains a path= query parameter, only files from that
        specific subdirectory are returned. Otherwise, all files are flattened
        from the root.

        Returns dict with keys: status, list, error
        Each item in list: filename, fs_id, size, is_dir, path, type
        """
        result = {"status": "failed", "list": [], "error": ""}

        # Step 1: Extract surl and optional path
        self.surl = _extract_surl(url)
        if not self.surl:
            result["error"] = "Could not extract share URL code"
            logger.warning(f"Terabox: could not extract surl from {url}")
            return result

        target_path = _extract_path(url)
        self.base_url = _get_base_url(url)
        logger.info(f"Terabox: surl={self.surl}, base={self.base_url}, path={target_path}")

        # Step 2: Get info from hnn workers proxy (for sign/timestamp/shareid/uk)
        hnn_data = self._hnn_get_info(self.surl)
        if not hnn_data:
            result["error"] = "Failed to get file info from proxy API"
            return result

        self.sign = hnn_data.get("sign", "")
        self.timestamp = str(hnn_data.get("timestamp", ""))
        self.shareid = str(hnn_data.get("shareid", ""))
        self.uk = str(hnn_data.get("uk", ""))
        self.randsk = hnn_data.get("randsk", "")

        # Step 3: Get file list
        all_files = []

        if target_path:
            # URL has path= param — use native API to list that specific directory
            logger.info(f"Terabox: targeting specific path: {target_path}")
            all_files = self._native_get_path_files(url, target_path)

            # Fallback: try hnn proxy for the targeted path
            if not all_files:
                children = self._hnn_list_dir(target_path)
                if children:
                    all_files = self._flatten_files(children)
        else:
            # No path param — flatten everything from root
            raw_list = hnn_data.get("list", [])
            all_files = self._flatten_files(raw_list)

            if not all_files:
                # Fallback: try native Terabox API with cookies
                logger.info("Terabox: hnn returned no files, trying native API")
                all_files = self._native_get_files(url)

        if not all_files:
            result["error"] = "No downloadable files found in this share link"
            return result

        result["status"] = "success"
        result["list"] = all_files
        return result

    def get_download_link(self, fs_id: str) -> str | None:
        """Generate a download link for a specific file."""
        if not self.sign or not self.timestamp:
            logger.warning("Terabox: no sign/timestamp available")
            return None

        params = {
            "shareid": self.shareid,
            "uk": self.uk,
            "sign": self.sign,
            "timestamp": self.timestamp,
            "fs_id": fs_id,
            "shorturl": self.surl,
            "randsk": self.randsk,
        }

        for endpoint in ["/api/get-download", "/api/get-downloadp"]:
            try:
                resp = self.sc.post(
                    f"{HNN_API_BASE}{endpoint}",
                    json=params,
                    headers={"Content-Type": "application/json"},
                    timeout=20,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    link = data.get("downloadLink", "")
                    if link:
                        logger.info(f"Terabox: got download link via {endpoint}")
                        return link
                    else:
                        logger.debug(f"Terabox: {endpoint}: {data.get('message', data.get('ok', '?'))}")
            except Exception as e:
                logger.warning(f"Terabox: {endpoint} failed: {e}")

        return None

    def download_file(
        self,
        fs_id: str,
        output_path: str,
        referer: str = "",
        max_retries: int = 3,
        progress_callback=None,
    ) -> dict | None:
        """
        Download a file from Terabox with retries and download-link regeneration.

        Primary: aria2c (handles CDN TCP resets, multiple connections, resume).
        Fallback: plain requests streaming.

        Returns dict with filepath/filename/size_mb on success, None on failure.
        """
        import os
        import shutil

        def _update(text):
            logger.info(text)
            if progress_callback:
                try:
                    progress_callback(text)
                except Exception:
                    pass

        aria2c_available = shutil.which("aria2c") is not None
        if aria2c_available:
            logger.info("Terabox: aria2c found — will use as primary downloader")
        else:
            logger.warning("Terabox: aria2c NOT found — falling back to requests (may fail on large files)")

        for attempt in range(1, max_retries + 1):
            # (Re)generate download link on every attempt — links are short-lived
            dl_url = self.get_download_link(fs_id)
            if not dl_url:
                logger.warning(f"Terabox download attempt {attempt}/{max_retries}: "
                               "failed to generate download link")
                if attempt < max_retries:
                    time.sleep(2)
                continue

            _update(f"⬇️ Downloading (attempt {attempt}/{max_retries})...")
            logger.debug(f"Terabox download URL: {dl_url[:150]}")

            # Clean up any leftover partial file (but NOT for aria2c which can resume)
            if not aria2c_available and os.path.exists(output_path):
                os.remove(output_path)

            success = False
            if aria2c_available:
                success = self._download_with_aria2c(dl_url, output_path, referer)
            if not success:
                success = self._download_with_requests(dl_url, output_path, referer, _update)

            if success and os.path.exists(output_path):
                file_size = os.path.getsize(output_path)

                # Reject HTML error pages
                with open(output_path, "rb") as f:
                    header = f.read(64)
                if b"<html" in header.lower() or b"<!doctype" in header.lower():
                    logger.warning(f"Terabox attempt {attempt}: downloaded HTML, not file")
                    os.remove(output_path)
                elif file_size > 1024:
                    size_mb = file_size / (1024 * 1024)
                    logger.info(f"Terabox: downloaded {size_mb:.2f} MB to {output_path}")
                    return {
                        "filepath": output_path,
                        "filename": os.path.basename(output_path),
                        "size_mb": round(size_mb, 2),
                    }
                else:
                    logger.warning(f"Terabox attempt {attempt}: file too small ({file_size} bytes)")
                    os.remove(output_path)

            if attempt < max_retries:
                wait = attempt * 2
                _update(f"⏳ Retrying in {wait}s...")
                time.sleep(wait)

        return None

    def _download_with_aria2c(self, url: str, output_path: str, referer: str = "") -> bool:
        """
        Download using aria2c — handles CDN TCP resets, supports multi-connection
        and automatic resume. Returns True on success.
        """
        import os
        import subprocess

        out_dir = os.path.dirname(output_path)
        out_file = os.path.basename(output_path)

        cmd = [
            "aria2c",
            "--out", out_file,
            "--dir", out_dir,
            "--max-connection-per-server=4",
            "--split=4",
            "--min-split-size=10M",
            "--max-tries=5",
            "--retry-wait=5",
            "--connect-timeout=15",
            "--timeout=120",
            "--continue=true",                 # resume partial downloads
            "--auto-file-renaming=false",
            "--allow-overwrite=true",
            "--console-log-level=warn",        # show warnings/errors
            "--summary-interval=0",            # suppress progress summary
            f"--user-agent={UA}",
            f"--referer={referer or 'https://www.terabox.app/'}",
            "--header=Accept: */*",
            "--header=Accept-Encoding: identity",
            url,
        ]
        logger.info(f"aria2c: starting download for {out_file}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            if result.returncode == 0 and os.path.exists(output_path):
                size_mb = os.path.getsize(output_path) / (1024 * 1024)
                logger.info(f"aria2c: success for {out_file} ({size_mb:.1f} MB)")
                return True
            logger.warning(f"aria2c failed (rc={result.returncode}): "
                           f"stderr={result.stderr[:300]} stdout={result.stdout[:200]}")
        except subprocess.TimeoutExpired:
            logger.warning("aria2c: timed out after 1800s")
        except Exception as e:
            logger.warning(f"aria2c error: {e}")
        return False

    def _download_with_requests(
        self, url: str, output_path: str, referer: str = "", progress_fn=None
    ) -> bool:
        """
        Download using plain requests streaming — fallback when aria2c unavailable.
        Returns True on success.
        """
        import os

        def _log(text):
            logger.info(text)
            if progress_fn:
                try:
                    progress_fn(text)
                except Exception:
                    pass

        dl_headers = {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "keep-alive",
            "Referer": referer or "https://www.terabox.app/",
        }
        try:
            resp = self.dl_session.get(
                url,
                headers=dl_headers,
                stream=True,
                timeout=(15, 300),
                allow_redirects=True,
            )
            logger.debug(
                f"requests: status={resp.status_code}, "
                f"content-length={resp.headers.get('content-length','?')}, "
                f"content-type={resp.headers.get('content-type','?')}"
            )
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "").lower()
            if "text/html" in content_type:
                resp.close()
                logger.warning("requests: got HTML response")
                return False

            total_bytes = int(resp.headers.get("content-length", 0))
            bytes_written = 0
            try:
                with open(output_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                            bytes_written += len(chunk)
                            if total_bytes and bytes_written % (1024 * 1024 * 10) < 1024 * 1024:
                                pct = bytes_written / total_bytes * 100
                                _log(f"⬇️ Progress: {pct:.0f}% "
                                     f"({bytes_written // 1024 // 1024} MB / "
                                     f"{total_bytes // 1024 // 1024} MB)")
            finally:
                resp.close()
            return bytes_written > 0
        except Exception as e:
            logger.warning(f"requests download failed: {e}", exc_info=False)
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except OSError:
                    pass
            return False

    def download_files(
        self,
        files: list[dict],
        download_dir: str,
        referer: str = "",
        max_workers: int = 3,
        progress_callback=None,
    ) -> list[dict]:
        """
        Download multiple Terabox files concurrently.

        Args:
            files: List of file dicts with keys: fs_id, filename, size, type
            download_dir: Directory to save files to
            referer: Referer URL for download headers
            max_workers: Max concurrent downloads (default 3)
            progress_callback: Callback for status updates

        Returns:
            List of successfully downloaded result dicts.
        """
        import os

        def _update(text):
            logger.info(text)
            if progress_callback:
                try:
                    progress_callback(text)
                except Exception:
                    pass

        total_count = len(files)
        results = [None] * total_count  # preserve order

        def _download_one(idx_and_file):
            idx, fileinfo = idx_and_file
            fname = fileinfo.get("filename", "unknown")
            safe_name = re.sub(r'[<>:"/\\|?*]', '_', fname)
            import uuid
            filename = f"{uuid.uuid4().hex[:8]}_{safe_name}"
            output_path = os.path.join(download_dir, filename)

            result = self.download_file(
                fs_id=fileinfo["fs_id"],
                output_path=output_path,
                referer=referer,
                max_retries=3,
                progress_callback=None,  # avoid noisy per-chunk updates from threads
            )
            return idx, fname, result

        _update(f"⚡ Downloading {total_count} files ({max_workers} parallel)...")

        completed = 0
        failed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_download_one, (i, f)): i
                for i, f in enumerate(files)
            }
            for future in as_completed(futures):
                try:
                    idx, fname, result = future.result()
                    if result:
                        results[idx] = result
                        completed += 1
                        size = result['size_mb']
                        _update(f"✅ [{completed + failed}/{total_count}] {fname} ({size:.1f} MB)")
                    else:
                        failed += 1
                        _update(f"⚠️ [{completed + failed}/{total_count}] Failed: {fname}")
                        logger.warning(f"Terabox: failed to download: {fname}")
                except Exception as e:
                    failed += 1
                    logger.warning(f"Terabox: download thread error: {e}")

        downloaded = [r for r in results if r is not None]
        _update(f"📊 Downloaded {len(downloaded)}/{total_count} files")
        return downloaded

    # ── HNN Workers Proxy Methods ────────────────────────────────────────

    def _hnn_get_info(self, surl: str) -> dict | None:
        """
        Get file info from hnn workers proxy.
        """
        api_url = f"{HNN_API_BASE}/api/get-info-new?shorturl={surl}&pwd="
        for attempt in range(3):
            try:
                resp = self.sc.get(api_url, timeout=15)
                if resp.status_code != 200:
                    logger.warning(f"Terabox: hnn get-info-new attempt {attempt + 1}: HTTP {resp.status_code}")
                    time.sleep(1.5)
                    continue

                # Log raw response snippet for diagnosis
                raw = resp.text
                logger.debug(f"Terabox: hnn get-info-new response (first 200): {raw[:200]}")

                try:
                    data = resp.json()
                except Exception as je:
                    logger.warning(f"Terabox: hnn get-info-new attempt {attempt + 1}: JSON parse failed: {je}; raw={raw[:100]}")
                    time.sleep(1.5)
                    continue

                if data.get("ok") and data.get("sign"):
                    logger.info(
                        f"Terabox: hnn OK "
                        f"(shareid={data.get('shareid')}, "
                        f"files={len(data.get('list', []))})"
                    )
                    return data
                else:
                    logger.warning(
                        f"Terabox: hnn get-info-new attempt {attempt + 1}: "
                        f"ok={data.get('ok')}, msg={data.get('message', '?')}"
                    )
            except Exception as e:
                logger.warning(f"Terabox: hnn get-info-new attempt {attempt + 1}: {e}")

            time.sleep(1.5)

        logger.warning("Terabox: all hnn info attempts failed")
        return None

    def _hnn_list_dir(self, dir_path: str) -> list | None:
        """List directory contents via hnn workers proxy."""
        try:
            url = (
                f"{HNN_API_BASE}/api/get-info-new"
                f"?shorturl={self.surl}"
                f"&dir={quote(dir_path)}"
                f"&randsk={quote(self.randsk)}"
            )
            resp = self.sc.get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok") and data.get("list"):
                    return data["list"]
                else:
                    logger.debug(f"Terabox: hnn list_dir: ok={data.get('ok')}")
        except Exception as e:
            logger.debug(f"Terabox: hnn list_dir failed for {dir_path[:50]}: {e}")
        return None

    # ── File Flattening ──────────────────────────────────────────────────

    def _flatten_files(self, file_list: list, depth: int = 0) -> list:
        """Recursively flatten directories into a list of downloadable files."""
        if depth > 3:
            return []

        result = []
        for item in file_list:
            is_dir = str(item.get("is_dir", item.get("isdir", "0")))
            if is_dir == "1" or is_dir == 1:
                dir_path = item.get("path", "")
                if dir_path:
                    # Try hnn proxy first
                    children = self._hnn_list_dir(dir_path)
                    if not children:
                        # Fallback: native API
                        children = self._native_list_dir(dir_path)
                    if children:
                        result.extend(self._flatten_files(children, depth + 1))
            else:
                filename = item.get("filename", item.get("server_filename", item.get("name", "unknown")))
                result.append({
                    "filename": filename,
                    "fs_id": str(item.get("fs_id", "")),
                    "size": int(item.get("size", 0)),
                    "path": item.get("path", ""),
                    "is_dir": False,
                    "type": _check_file_type(filename),
                })
        return result

    # ── Native Terabox API Methods (cookie-based) ────────────────────────

    def _native_get_path_files(self, url: str, target_path: str) -> list:
        """Get files from a specific path using native Terabox API."""
        try:
            # Visit share page to establish cookies
            self.native_session.get(url, allow_redirects=True, timeout=15)
            base = self.base_url or "https://www.terabox.app"

            # Get shareid/uk from shorturlinfo
            api_url = f"{base}/api/shorturlinfo?app_id=250528&shorturl=1{self.surl}&root=1"
            resp = self.native_session.get(api_url, timeout=15)
            data = resp.json()
            if data.get("errno", 0) != 0:
                logger.warning(f"Terabox: native API errno={data.get('errno')}")
                return []

            if not self.shareid and data.get("shareid"):
                self.shareid = str(data["shareid"])
            if not self.uk and data.get("uk"):
                self.uk = str(data["uk"])

            # List the specific directory
            children = self._native_list_dir(target_path, base)
            if children:
                return self._flatten_native_list(children, base)

            return []
        except Exception as e:
            logger.warning(f"Terabox: native path API failed: {e}")
            return []

    def _native_get_files(self, url: str) -> list:
        """Get files using native Terabox API with session cookies."""
        try:
            # Visit share page to establish cookies
            self.native_session.get(url, allow_redirects=True, timeout=15)
            base = self.base_url or "https://www.terabox.app"

            # Get root file list
            api_url = f"{base}/api/shorturlinfo?app_id=250528&shorturl=1{self.surl}&root=1"
            resp = self.native_session.get(api_url, timeout=15)
            data = resp.json()

            if data.get("errno", 0) != 0:
                logger.warning(f"Terabox: native API errno={data.get('errno')}: {data.get('errmsg', '?')}")
                return []

            if not self.shareid and data.get("shareid"):
                self.shareid = str(data["shareid"])
            if not self.uk and data.get("uk"):
                self.uk = str(data["uk"])

            return self._flatten_native_list(data.get("list", []), base)

        except Exception as e:
            logger.warning(f"Terabox: native API failed: {e}")
            return []

    def _flatten_native_list(self, file_list: list, base: str, depth: int = 0) -> list:
        """Recursively flatten native API file list."""
        if depth > 3:
            return []

        result = []
        for item in file_list:
            is_dir = bool(int(item.get("isdir", 0)))
            if is_dir:
                path = item.get("path", "")
                if path:
                    children = self._native_list_dir(path, base)
                    if children:
                        result.extend(self._flatten_native_list(children, base, depth + 1))
            else:
                filename = item.get("server_filename", "unknown")
                result.append({
                    "filename": filename,
                    "fs_id": str(item.get("fs_id", "")),
                    "size": int(item.get("size", 0)),
                    "path": item.get("path", ""),
                    "is_dir": False,
                    "type": _check_file_type(filename),
                })
        return result

    def _native_list_dir(self, dir_path: str, base: str = None) -> list | None:
        """List directory contents via native Terabox API."""
        base = base or self.base_url or "https://www.terabox.app"
        try:
            # Note: native share/list uses surl WITHOUT '1' prefix
            url = f"{base}/share/list?app_id=250528&shorturl={self.surl}&root=0&dir={quote(dir_path)}"
            resp = self.native_session.get(url, timeout=15)
            data = resp.json()
            if data.get("errno") == 0 and data.get("list"):
                return data["list"]
            else:
                logger.debug(f"Terabox: native list_dir errno={data.get('errno')}")
        except Exception as e:
            logger.debug(f"Terabox: native list_dir failed for {dir_path[:50]}: {e}")
        return None
