from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime
import logging
from collections.abc import Callable
import re

from yt_dlp import YoutubeDL

from .models import VideoItem


log = logging.getLogger(__name__)
MIN_MEDIA_BYTES = 256 * 1024


def validate_media_file(path: Path) -> tuple[bool, str]:
    try:
        size = path.stat().st_size
        if size < MIN_MEDIA_BYTES:
            return False, f"媒体文件只有 {size} B，疑似 CDN 空响应"
        with path.open("rb") as handle:
            head = handle.read(1024).lstrip().lower()
        if head.startswith((b"<!doctype", b"<html", b"<?xml")):
            return False, "CDN 返回了 HTML/XML 错误页"
        if path.suffix.lower() == ".mp4" and b"ftyp" not in head[:64]:
            return False, "MP4 文件头无效"
        return True, ""
    except OSError as exc:
        return False, f"无法校验媒体文件：{exc}"


class BatchDownloader:
    def __init__(
        self,
        output_dir: Path,
        workers: int = 2,
        fragments: int = 4,
        cookies: Path | None = None,
        rate_limit: int = 0,
        folder_mode: str = "flat",
        progress_callback: Callable[[VideoItem, dict], None] | None = None,
    ):
        self.output_dir = output_dir
        self.workers = workers
        self.fragments = fragments
        self.cookies = cookies
        self.rate_limit = rate_limit
        self.folder_mode = folder_mode
        self.progress_callback = progress_callback
        output_dir.mkdir(parents=True, exist_ok=True)

    def _download_one(self, item: VideoItem) -> tuple[VideoItem, str | None]:
        url = item.stream_url or item.page_url
        target_dir = self.output_dir
        date_folder = datetime.now().strftime("%Y-%m-%d")
        category_folder = re.sub(r'[^A-Za-z0-9._-]+', "_", item.source or "uncategorized").strip("._") or "uncategorized"
        if self.folder_mode == "date":
            target_dir /= date_folder
        elif self.folder_mode == "category":
            target_dir /= category_folder
        elif self.folder_mode == "date_category":
            target_dir = target_dir / date_folder / category_folder
        target_dir.mkdir(parents=True, exist_ok=True)
        options = {
            "outtmpl": str(target_dir / f"{item.filename}.%(ext)s"),
            "continuedl": True,
            "overwrites": True,
            "concurrent_fragment_downloads": self.fragments,
            "retries": 10,
            "fragment_retries": 10,
            "file_access_retries": 5,
            "http_headers": {"Referer": item.page_url},
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
        }
        if self.progress_callback:
            options["progress_hooks"] = [lambda data: self.progress_callback(item, data)]
        if self.rate_limit:
            options["ratelimit"] = self.rate_limit
        if self.cookies:
            options["cookiefile"] = str(self.cookies)
        try:
            with YoutubeDL(options) as ydl:
                ydl.download([url])
            files = [
                path for path in target_dir.iterdir()
                if f"[{item.identity}]" in path.name
                if path.is_file() and path.suffix.lower() not in {".part", ".ytdl", ".temp"}
            ]
            if not files:
                return item, "下载器未生成媒体文件"
            output = max(files, key=lambda path: path.stat().st_mtime)
            valid, reason = validate_media_file(output)
            if not valid:
                try:
                    output.unlink()
                except OSError:
                    pass
                return item, reason
            return item, None
        except Exception as exc:  # yt-dlp wraps network and ffmpeg failures.
            error = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))
            return item, error

    def download(self, items: list[VideoItem]) -> list[tuple[VideoItem, str | None]]:
        results: list[tuple[VideoItem, str | None]] = []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self._download_one, item): item for item in items}
            for future in as_completed(futures):
                item, error = future.result()
                log.error("下载失败 %s: %s", item.page_url, error) if error else log.info("完成 %s", item.filename)
                results.append((item, error))
        return results
