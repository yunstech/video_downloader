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
from urllib.parse import quote, urlparse, parse_qs

import cloudscraper
import requests

logger = logging.getLogger(__name__)

TERABOX_DOMAINS = (
    "terabox.com", "1024terabox.com", "freeterabox.com", "nephobox.com",
    "terabox.app", "teraboxapp.com", "4funbox.com", "mirrobox.com",
    "momerybox.com", "teraboxlink.com",
)

HNN_API_BASE = "https://terabox.hnn.workers.dev"
HNN_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "referer": f"{HNN_API_BASE}/",
    "origin": HNN_API_BASE,
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/134.0.0.0 Safari/537.36"
    ),
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


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
        self.sc = cloudscraper.create_scraper()
        self.native_session = requests.Session()
        self.native_session.headers.update({"User-Agent": UA})
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

        Returns dict with keys: status, list, error
        Each item in list: filename, fs_id, size, is_dir, path, type
        """
        result = {"status": "failed", "list": [], "error": ""}

        # Step 1: Extract surl
        self.surl = _extract_surl(url)
        if not self.surl:
            result["error"] = "Could not extract share URL code"
            logger.warning(f"Terabox: could not extract surl from {url}")
            return result

        self.base_url = _get_base_url(url)
        logger.info(f"Terabox: surl={self.surl}, base={self.base_url}")

        # Step 2: Get info from hnn workers proxy
        hnn_data = self._hnn_get_info(self.surl)
        if not hnn_data:
            result["error"] = "Failed to get file info from proxy API"
            return result

        self.sign = hnn_data.get("sign", "")
        self.timestamp = str(hnn_data.get("timestamp", ""))
        self.shareid = str(hnn_data.get("shareid", ""))
        self.uk = str(hnn_data.get("uk", ""))
        self.randsk = hnn_data.get("randsk", "")

        # Step 3: Flatten file list (navigate subdirectories)
        raw_list = hnn_data.get("list", [])
        all_files = self._flatten_files(raw_list)

        if not all_files:
            # Fallback: try native Terabox API with cookies
            logger.info("Terabox: hnn returned no files in subdirs, trying native API")
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
                    headers=HNN_HEADERS,
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

        Uses the cloudscraper session (TLS fingerprint + cookies) which is
        required by the Terabox CDN. Regenerates the download link on each
        retry because Terabox links are short-lived and often single-use.

        Returns dict with filepath/filename/size_mb on success, None on failure.
        """
        import os

        def _update(text):
            logger.info(text)
            if progress_callback:
                try:
                    progress_callback(text)
                except Exception:
                    pass

        dl_headers = {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "keep-alive",
            "Referer": referer or f"https://www.terabox.app/",
        }

        for attempt in range(1, max_retries + 1):
            # (Re)generate download link on every attempt
            dl_url = self.get_download_link(fs_id)
            if not dl_url:
                logger.warning(f"Terabox download attempt {attempt}/{max_retries}: "
                               "failed to generate download link")
                if attempt < max_retries:
                    time.sleep(2)
                continue

            try:
                _update(f"⬇️ Downloading (attempt {attempt}/{max_retries})...")
                logger.debug(f"Terabox download URL: {dl_url[:150]}")

                # Use cloudscraper session for TLS fingerprint compatibility
                resp = self.sc.get(
                    dl_url,
                    headers=dl_headers,
                    stream=True,
                    timeout=120,
                    allow_redirects=True,
                )
                logger.debug(
                    f"Terabox download response: status={resp.status_code}, "
                    f"content-type={resp.headers.get('content-type', '?')}, "
                    f"content-length={resp.headers.get('content-length', '?')}"
                )
                resp.raise_for_status()

                # Reject HTML error pages
                content_type = resp.headers.get("content-type", "").lower()
                if "text/html" in content_type:
                    logger.warning(f"Terabox attempt {attempt}: got HTML instead of file")
                    if attempt < max_retries:
                        time.sleep(2)
                    continue

                total_bytes = int(resp.headers.get("content-length", 0))
                bytes_written = 0

                with open(output_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            bytes_written += len(chunk)
                            if total_bytes and bytes_written % (1024 * 1024 * 5) < 65536:
                                pct = bytes_written / total_bytes * 100
                                _update(f"⬇️ Progress: {pct:.0f}% "
                                        f"({bytes_written // 1024 // 1024} MB / "
                                        f"{total_bytes // 1024 // 1024} MB)")

                # Validate downloaded file
                if os.path.exists(output_path):
                    file_size = os.path.getsize(output_path)

                    # Check for HTML error pages disguised as files
                    with open(output_path, "rb") as f:
                        header = f.read(64)
                    if b"<html" in header.lower() or b"<!doctype" in header.lower():
                        logger.warning(f"Terabox attempt {attempt}: downloaded HTML, not file")
                        os.remove(output_path)
                        if attempt < max_retries:
                            time.sleep(2)
                        continue

                    if file_size > 1024:  # at least 1 KB
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

            except Exception as e:
                logger.warning(
                    f"Terabox download attempt {attempt}/{max_retries} failed: {e}",
                    exc_info=(attempt == max_retries),
                )
                # Clean up partial file
                if os.path.exists(output_path):
                    try:
                        os.remove(output_path)
                    except OSError:
                        pass

            if attempt < max_retries:
                wait = attempt * 2
                _update(f"⏳ Retrying in {wait}s...")
                time.sleep(wait)

        return None

    # ── HNN Workers Proxy Methods ────────────────────────────────────────

    def _hnn_get_info(self, surl: str) -> dict | None:
        """Get file info from hnn workers proxy with retries."""
        # Visit main page first to get CF cookies
        try:
            self.sc.get(HNN_API_BASE + "/", timeout=10)
        except Exception as e:
            logger.debug(f"Terabox: hnn main page: {e}")

        api_url = f"{HNN_API_BASE}/api/get-info-new?shorturl={surl}&pwd="

        for attempt in range(3):
            try:
                resp = self.sc.get(api_url, headers=HNN_HEADERS, timeout=15)
                if resp.status_code != 200:
                    logger.debug(f"Terabox: hnn attempt {attempt + 1}: HTTP {resp.status_code}")
                    time.sleep(1.5)
                    continue

                data = resp.json()
                if data.get("ok") and data.get("sign"):
                    logger.info(
                        f"Terabox: hnn OK (shareid={data.get('shareid')}, "
                        f"files={len(data.get('list', []))})"
                    )
                    return data
                else:
                    logger.debug(
                        f"Terabox: hnn attempt {attempt + 1}: ok={data.get('ok')}, "
                        f"msg={data.get('message', '?')}"
                    )
            except Exception as e:
                logger.debug(f"Terabox: hnn attempt {attempt + 1}: {e}")

            time.sleep(1.5)

        logger.warning("Terabox: hnn get-info-new failed after 3 attempts")
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
            resp = self.sc.get(url, headers=HNN_HEADERS, timeout=15)
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
