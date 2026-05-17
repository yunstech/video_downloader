"""
Terabox download module.

Primary approach: uses Playwright to capture jsToken + pcftoken from the
Terabox share page, then downloads via dm.terabox.com (no account required).
Fallback: BDUSS/STOKEN authenticated session (account required).
"""
import re
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urlparse, parse_qs, urlencode

import requests
from requests.adapters import HTTPAdapter

from src import config

logger = logging.getLogger(__name__)

TERABOX_DOMAINS = (
    "terabox.com", "1024terabox.com", "freeterabox.com", "nephobox.com",
    "terabox.app", "teraboxapp.com", "4funbox.com", "mirrobox.com",
    "momerybox.com", "teraboxlink.com",
)

BASE = "https://www.terabox.com"
DM_BASE = "https://dm.terabox.com"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36"
)


def _extract_surl(url: str) -> str | None:
    """Extract the short URL code from a Terabox sharing link."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "surl" in qs:
        return qs["surl"][0]

    m = re.search(r"/s/1([A-Za-z0-9_-]+)", url)
    if m:
        return m.group(1)

    try:
        resp = requests.get(url, headers={"User-Agent": UA}, allow_redirects=True, timeout=15)
        m = re.search(r"surl=([^&]+)", resp.url)
        if m:
            return m.group(1)
    except Exception as e:
        logger.warning(f"Terabox: redirect failed: {e}")

    return None


def _extract_path(url: str) -> str | None:
    """Extract optional path= parameter from a Terabox sharing link."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    return qs["path"][0] if "path" in qs else None


def _check_file_type(name: str) -> str:
    name = name.lower()
    if any(ext in name for ext in [".mp4", ".mov", ".m4v", ".mkv", ".asf", ".avi", ".wmv", ".m2ts", ".3g2", ".webm"]):
        return "video"
    elif any(ext in name for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"]):
        return "image"
    elif any(ext in name for ext in [".mp3", ".aac", ".flac", ".ogg", ".wav"]):
        return "audio"
    elif any(ext in name for ext in [".pdf", ".docx", ".zip", ".rar", ".7z"]):
        return "file"
    return "other"


class TeraboxDownloader:
    """
    Downloads files from Terabox share links.

    Primary flow (no account needed):
    1. Playwright loads the share page and intercepts /api/shorturlinfo
    2. Captures jsToken (from request URL), pcftoken (from cookies),
       and share metadata (sign, timestamp, shareid, uk, file list)
    3. Builds dm.terabox.com/share/download URLs with captured tokens
    4. Downloads via aria2c or streaming requests

    Fallback (BDUSS/STOKEN required):
    1. Authenticated session using TERABOX_BDUSS cookie
    2. Calls /api/shorturlinfo and /share/download directly
    """

    def __init__(self):
        self.bduss = config.TERABOX_BDUSS
        self.stoken = config.TERABOX_STOKEN

        # Share metadata (populated by get_file_info)
        self.sign = ""
        self.timestamp = ""
        self.shareid = ""
        self.uk = ""
        self.randsk = ""
        self.surl = ""
        self.js_token = ""   # From Playwright network intercept
        self.pfc_token = ""  # pcftoken from Playwright cookies

        # Session for API calls
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        })
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        # Separate plain session for large CDN downloads
        self.dl_session = requests.Session()
        self.dl_session.headers.update({"User-Agent": UA})
        adapter2 = HTTPAdapter(pool_connections=4, pool_maxsize=4)
        self.dl_session.mount("https://", adapter2)
        self.dl_session.mount("http://", adapter2)

        if self.bduss:
            for s in (self.session, self.dl_session):
                s.cookies.set("BDUSS", self.bduss, domain=".terabox.com")
        if self.stoken:
            for s in (self.session, self.dl_session):
                s.cookies.set("STOKEN", self.stoken, domain=".terabox.com")

    def get_file_info(self, url: str) -> dict:
        """
        Get file info from a Terabox share URL.

        Returns dict with keys: status, list, error
        Each item in list: filename, fs_id, size, is_dir, path, type
        """
        result = {"status": "failed", "list": [], "error": ""}

        self.surl = _extract_surl(url)
        if not self.surl:
            result["error"] = "Could not extract share URL code from the given URL"
            return result

        target_path = _extract_path(url)
        logger.info(f"Terabox: surl={self.surl}, path={target_path}")

        # Strategy 1: Playwright (jsToken + pcftoken, no account required)
        pw_data = self._fetch_via_playwright(url)
        if pw_data:
            self.sign = pw_data.get("sign", "")
            self.timestamp = str(pw_data.get("timestamp", ""))
            self.shareid = str(pw_data.get("shareid", ""))
            self.uk = str(pw_data.get("uk", ""))
            self.randsk = pw_data.get("randsk", "")
            self.js_token = pw_data.get("jsToken", "")
            self.pfc_token = pw_data.get("pcftoken", "")

            # Apply browser cookies to both sessions for subsequent API calls
            for name, value in pw_data.get("cookies", {}).items():
                self.session.cookies.set(name, value, domain=".terabox.com")
                self.dl_session.cookies.set(name, value, domain=".terabox.com")

            raw_list = pw_data.get("list", [])
            if target_path:
                all_files = self._list_dir(target_path)
            else:
                all_files = self._flatten_files(raw_list)

            if all_files:
                result["status"] = "success"
                result["list"] = all_files
                return result

            logger.warning("Terabox Playwright: no files found in list, trying BDUSS fallback")

        # Strategy 2: BDUSS/STOKEN (authenticated session)
        if not self.bduss:
            result["error"] = (
                "Terabox: Playwright could not capture required tokens.\n"
                "Set TERABOX_BDUSS to enable authenticated fallback:\n"
                "1. Log into terabox.com in your browser\n"
                "2. DevTools -> Application -> Cookies -> terabox.com\n"
                "3. Copy the BDUSS value\n"
                "4. Set environment variable: TERABOX_BDUSS=<value>"
            )
            return result

        info = self._get_share_info_bduss()
        if not info:
            result["error"] = (
                "Failed to get share info from Terabox. "
                "TERABOX_BDUSS may be invalid or expired."
            )
            return result

        self.sign = info.get("sign", "")
        self.timestamp = str(info.get("timestamp", ""))
        self.shareid = str(info.get("shareid", ""))
        self.uk = str(info.get("uk", ""))
        self.randsk = info.get("randsk", "")

        raw_list = info.get("list", [])
        if target_path:
            all_files = self._list_dir(target_path)
        else:
            all_files = self._flatten_files(raw_list)

        if not all_files:
            result["error"] = "No downloadable files found in this share link"
            return result

        result["status"] = "success"
        result["list"] = all_files
        return result

    def get_download_link(self, fs_id: str) -> str | None:
        """Get a direct CDN download URL for a file by fs_id."""
        if self.js_token and self.pfc_token and self.sign:
            link = self._get_download_link_dm(fs_id)
            if link:
                return link
            logger.warning("Terabox: dm approach failed, falling back to BDUSS")

        if self.bduss and self.sign:
            return self._get_download_link_bduss(fs_id)

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
        Download a Terabox file. Returns dict with filepath/filename/size_mb or None.

        Primary downloader: aria2c (handles CDN TCP resets, multi-connection, resume).
        Fallback: streaming requests.
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

        for attempt in range(1, max_retries + 1):
            dl_url = self.get_download_link(fs_id)
            if not dl_url:
                logger.warning(f"Terabox attempt {attempt}/{max_retries}: no download link")
                if attempt < max_retries:
                    time.sleep(2)
                continue

            _update(f"⬇️ Downloading (attempt {attempt}/{max_retries})...")
            logger.debug(f"Terabox download URL: {dl_url[:150]}")

            if not aria2c_available and os.path.exists(output_path):
                os.remove(output_path)

            success = False
            if aria2c_available:
                success = self._download_with_aria2c(dl_url, output_path, referer)
            if not success:
                success = self._download_with_requests(dl_url, output_path, referer, _update)

            if success and os.path.exists(output_path):
                file_size = os.path.getsize(output_path)

                with open(output_path, "rb") as f:
                    header = f.read(64)
                if b"<html" in header.lower() or b"<!doctype" in header.lower():
                    logger.warning(f"Terabox attempt {attempt}: got HTML response")
                    os.remove(output_path)
                elif file_size > 1024:
                    size_mb = file_size / (1024 * 1024)
                    logger.info(f"Terabox: downloaded {size_mb:.2f} MB -> {output_path}")
                    return {
                        "filepath": output_path,
                        "filename": os.path.basename(output_path),
                        "size_mb": round(size_mb, 2),
                    }
                else:
                    logger.warning(f"Terabox attempt {attempt}: file too small ({file_size} B)")
                    os.remove(output_path)

            if attempt < max_retries:
                wait = attempt * 2
                _update(f"⏳ Retrying in {wait}s...")
                time.sleep(wait)

        return None

    def _download_with_aria2c(self, url: str, output_path: str, referer: str = "") -> bool:
        import os, subprocess

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
            "--continue=true",
            "--auto-file-renaming=false",
            "--allow-overwrite=true",
            "--console-log-level=warn",
            "--summary-interval=0",
            f"--user-agent={UA}",
            f"--referer={referer or BASE + '/'}",
            "--header=Accept: */*",
            "--header=Accept-Encoding: identity",
            url,
        ]
        logger.info(f"aria2c: downloading {out_file}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            if result.returncode == 0 and os.path.exists(output_path):
                size_mb = os.path.getsize(output_path) / (1024 * 1024)
                logger.info(f"aria2c: success ({size_mb:.1f} MB)")
                return True
            logger.warning(f"aria2c failed (rc={result.returncode}): {result.stderr[:300]}")
        except subprocess.TimeoutExpired:
            logger.warning("aria2c: timed out after 1800s")
        except Exception as e:
            logger.warning(f"aria2c error: {e}")
        return False

    def _download_with_requests(
        self, url: str, output_path: str, referer: str = "", progress_fn=None
    ) -> bool:
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
            "Referer": referer or BASE + "/",
        }
        try:
            resp = self.dl_session.get(url, headers=dl_headers, stream=True, timeout=(15, 300), allow_redirects=True)
            logger.debug(f"requests: status={resp.status_code}, content-type={resp.headers.get('content-type','?')}")
            resp.raise_for_status()

            if "text/html" in resp.headers.get("content-type", "").lower():
                resp.close()
                logger.warning("requests: got HTML response (auth failure?)")
                return False

            total_bytes = int(resp.headers.get("content-length", 0))
            bytes_written = 0
            try:
                with open(output_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                            bytes_written += len(chunk)
                            if total_bytes and bytes_written % (10 * 1024 * 1024) < 1024 * 1024:
                                pct = bytes_written / total_bytes * 100
                                _log(f"⬇️ Progress: {pct:.0f}% ({bytes_written//1024//1024} / {total_bytes//1024//1024} MB)")
            finally:
                resp.close()
            return bytes_written > 0
        except Exception as e:
            logger.warning(f"requests download failed: {e}")
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
        max_workers: int = 1,
        progress_callback=None,
    ) -> list[dict]:
        """Download multiple Terabox files. Returns list of result dicts."""
        import os, uuid

        def _update(text):
            logger.info(text)
            if progress_callback:
                try:
                    progress_callback(text)
                except Exception:
                    pass

        total = len(files)
        results = [None] * total

        def _download_one(idx_and_file):
            idx, fileinfo = idx_and_file
            fname = fileinfo.get("filename", "unknown")
            safe_name = re.sub(r'[<>:"/\\|?*]', '_', fname)
            output_path = os.path.join(download_dir, f"{uuid.uuid4().hex[:8]}_{safe_name}")
            result = self.download_file(
                fs_id=fileinfo["fs_id"],
                output_path=output_path,
                referer=referer,
                max_retries=3,
            )
            return idx, fname, result

        _update(f"⚡ Downloading {total} file(s)...")

        completed = failed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_download_one, (i, f)): i for i, f in enumerate(files)}
            for future in as_completed(futures):
                try:
                    idx, fname, result = future.result()
                    if result:
                        results[idx] = result
                        completed += 1
                        _update(f"✅ [{completed + failed}/{total}] {fname} ({result['size_mb']:.1f} MB)")
                    else:
                        failed += 1
                        _update(f"⚠️ [{completed + failed}/{total}] Failed: {fname}")
                except Exception as e:
                    failed += 1
                    logger.warning(f"Terabox: download thread error: {e}")

        downloaded = [r for r in results if r is not None]
        _update(f"📊 Downloaded {len(downloaded)}/{total} files")
        return downloaded

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _fetch_via_playwright(self, share_url: str) -> dict | None:
        """
        Launch headless Chromium, load the share page, and capture:
        - jsToken (from /api/shorturlinfo request URL or window.jsToken)
        - pcftoken (from cookies)
        - sign, timestamp, shareid, uk, file list (from shorturlinfo response)
        - all session cookies for subsequent requests
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("Terabox: playwright not installed")
            return None

        captured = {}

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=UA,
                    viewport={"width": 1280, "height": 720},
                )
                page = context.new_page()

                def on_request(request):
                    req_url = request.url
                    if "shorturlinfo" in req_url or "shareinfo" in req_url:
                        try:
                            parsed = urlparse(req_url)
                            qs = parse_qs(parsed.query)
                            if "jsToken" in qs and not captured.get("jsToken"):
                                captured["jsToken"] = qs["jsToken"][0]
                                logger.debug(f"Terabox: jsToken from request URL ({len(captured['jsToken'])} chars)")
                        except Exception as e:
                            logger.debug(f"Terabox: request intercept error: {e}")

                def on_response(response):
                    resp_url = response.url
                    if "shorturlinfo" in resp_url or "shareinfo" in resp_url:
                        try:
                            body = response.json()
                            if body.get("errno") == 0:
                                captured.update({
                                    "sign": body.get("sign", ""),
                                    "timestamp": str(body.get("timestamp", "")),
                                    "shareid": str(body.get("shareid", "")),
                                    "uk": str(body.get("uk", "")),
                                    "randsk": body.get("randsk", ""),
                                    "list": body.get("list", []),
                                })
                                logger.info(
                                    f"Terabox: shorturlinfo intercepted: "
                                    f"shareid={captured.get('shareid')}, "
                                    f"files={len(captured.get('list', []))}"
                                )
                            else:
                                logger.warning(f"Terabox: shorturlinfo errno={body.get('errno')}: {body.get('errmsg','')}")
                        except Exception as e:
                            logger.debug(f"Terabox: response parse error: {e}")

                page.on("request", on_request)
                page.on("response", on_response)

                try:
                    page.goto(share_url, wait_until="networkidle", timeout=35000)
                except Exception as e:
                    # Partial loads (timeout on non-critical resources) are acceptable
                    logger.warning(f"Terabox Playwright: page load warning (partial load OK): {e}")

                # Try window.jsToken if not captured from network intercept
                if not captured.get("jsToken"):
                    for js_expr in ("window.jsToken", "window.YZToken"):
                        try:
                            val = page.evaluate(js_expr)
                            if val and isinstance(val, str) and len(val) > 20:
                                captured["jsToken"] = val
                                logger.debug(f"Terabox: jsToken from {js_expr} ({len(val)} chars)")
                                break
                        except Exception:
                            pass

                # Capture all cookies
                cookies_dict = {}
                for c in context.cookies():
                    cookies_dict[c["name"]] = c["value"]

                if "pcftoken" in cookies_dict:
                    captured["pcftoken"] = cookies_dict["pcftoken"]
                elif "csrfToken" in cookies_dict:
                    # csrfToken is functionally equivalent to pcftoken in some flows
                    captured["pcftoken"] = cookies_dict["csrfToken"]

                captured["cookies"] = cookies_dict
                browser.close()

        except Exception as e:
            logger.warning(f"Terabox Playwright: unexpected error: {e}")
            return None

        has_token = bool(captured.get("jsToken"))
        has_share = bool(captured.get("sign"))
        logger.info(
            f"Terabox Playwright: jsToken={has_token}, share_info={has_share}, "
            f"cookies={len(captured.get('cookies', {}))}, pcftoken={'pcftoken' in captured}"
        )

        if not has_token or not has_share:
            logger.warning(
                f"Terabox Playwright: incomplete capture — "
                f"jsToken={'jsToken' in captured}, sign={'sign' in captured}"
            )
            return None

        return captured

    def _get_download_link_dm(self, fs_id: str) -> str | None:
        """Get download URL via dm.terabox.com using jsToken + pcftoken."""
        params = {
            "clientfrom": "h5",
            "psign": "0",
            "pcftoken": self.pfc_token,
            "clienttype": "0",
            "channel": "dubox",
            "scene": "",
            "shareid": self.shareid,
            "sign": self.sign,
            "timestamp": self.timestamp,
            "app_id": "250528",
            "web": "1",
            "jsToken": self.js_token,
            "fs_id": fs_id,
            "uk": self.uk,
        }
        url = f"{DM_BASE}/share/download?{urlencode(params)}"

        try:
            resp = self.session.get(
                url,
                allow_redirects=False,
                timeout=20,
                headers={"Referer": BASE + "/"},
            )
            logger.debug(f"Terabox dm: status={resp.status_code}, body={resp.text[:300]}")

            if resp.status_code in (301, 302, 307, 308):
                location = resp.headers.get("Location", "")
                if location:
                    return location

            if resp.status_code == 200:
                data = resp.json()
                if data.get("errno") == 0:
                    return data.get("dlink") or data.get("downloadLink") or ""
                err = data.get("errno")
                if err == 400310:
                    logger.warning("Terabox dm: verify_v2 triggered — jsToken/pcftoken may be stale")
                else:
                    logger.warning(f"Terabox dm: errno={err}: {data.get('errmsg', '')}")

        except Exception as e:
            logger.warning(f"Terabox dm download link failed: {e}")

        return None

    def _get_download_link_bduss(self, fs_id: str) -> str | None:
        """Get download URL via BDUSS-authenticated session (fallback)."""
        params = (
            f"sign={self.sign}&timestamp={self.timestamp}"
            f"&fs_id={fs_id}&shareid={self.shareid}&uk={self.uk}"
            f"&channel=dubox&web=1&app_id=250528"
        )
        if self.randsk:
            params += f"&randsk={quote(self.randsk)}"

        url = f"{BASE}/share/download?{params}"
        try:
            resp = self.session.get(url, allow_redirects=False, timeout=20)
            logger.debug(f"Terabox bduss: status={resp.status_code}, body={resp.text[:200]}")

            if resp.status_code in (301, 302, 307, 308):
                return resp.headers.get("Location", "")

            if resp.status_code == 200:
                data = resp.json()
                if data.get("errno") == 0:
                    return data.get("dlink") or data.get("downloadLink") or ""
                err = data.get("errno")
                if err == 400310:
                    logger.warning(
                        "Terabox: verify_v2 required — BDUSS may be expired. "
                        "Re-login to terabox.com and update TERABOX_BDUSS."
                    )
                else:
                    logger.warning(f"Terabox: bduss errno={err}: {data.get('errmsg', '')}")

        except Exception as e:
            logger.warning(f"Terabox bduss download link failed: {e}")

        return None

    def _get_share_info_bduss(self) -> dict | None:
        """Call /api/shorturlinfo with BDUSS session and return the JSON data."""
        url = f"{BASE}/api/shorturlinfo?app_id=250528&shorturl=1{self.surl}&root=1"
        for attempt in range(3):
            try:
                resp = self.session.get(url, timeout=15)
                if resp.status_code != 200:
                    logger.warning(f"Terabox: shorturlinfo attempt {attempt+1}: HTTP {resp.status_code}")
                    time.sleep(1.5)
                    continue
                data = resp.json()
                if data.get("errno") == 0:
                    logger.info(f"Terabox: shorturlinfo OK (shareid={data.get('shareid')}, files={len(data.get('list',[]))})")
                    return data
                logger.warning(f"Terabox: shorturlinfo errno={data.get('errno')}: {data.get('errmsg', '?')}")
            except Exception as e:
                logger.warning(f"Terabox: shorturlinfo attempt {attempt+1}: {e}")
            time.sleep(1.5)
        return None

    def _list_dir(self, dir_path: str) -> list:
        """List all files (recursively) under a directory path using /share/list."""
        url = (
            f"{BASE}/share/list?app_id=250528&shorturl={self.surl}"
            f"&dir={quote(dir_path)}&root=0&num=200&by=time"
        )
        try:
            resp = self.session.get(url, timeout=15)
            data = resp.json()
            if data.get("errno") == 0 and data.get("list"):
                return self._flatten_files(data["list"])
            logger.warning(f"Terabox: share/list errno={data.get('errno')}")
        except Exception as e:
            logger.warning(f"Terabox: share/list failed for {dir_path[:50]}: {e}")
        return []

    def _flatten_files(self, file_list: list, depth: int = 0) -> list:
        """Recursively flatten a Terabox file list, expanding directories."""
        if depth > 4:
            return []

        result = []
        for item in file_list:
            is_dir = str(item.get("is_dir", item.get("isdir", item.get("isDir", "0"))))
            if is_dir in ("1", "true"):
                dir_path = item.get("path", "")
                if dir_path:
                    children = self._list_dir(dir_path)
                    if children:
                        result.extend(self._flatten_files(children, depth + 1))
            else:
                filename = item.get(
                    "server_filename",
                    item.get("filename", item.get("name", "unknown"))
                )
                result.append({
                    "filename": filename,
                    "fs_id": str(item.get("fs_id", "")),
                    "size": int(item.get("size", 0)),
                    "path": item.get("path", ""),
                    "is_dir": False,
                    "type": _check_file_type(filename),
                })
        return result
