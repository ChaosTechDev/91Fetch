from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from datetime import datetime
from threading import Event, Lock, Thread, Timer
from time import time
from typing import Literal
from urllib.parse import parse_qs, urljoin, urlparse
import json
import os
import re
import socket
import uuid
import webbrowser

from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

from .crawler import Crawler, fresh_listing_url, listing_page_url
from .downloader import BatchDownloader, validate_media_file
from .http import build_client, load_cookies
from .models import SiteConfig, VideoItem
from .parser import parse_listing


ROOT = Path.cwd()
STATIC_DIR = Path(__file__).parent / "static"
CONFIG_PATH = ROOT / "site.json"
DATA_DIR = Path(os.getenv("VIEWKEY_DATA_DIR", str(ROOT / "downloads")))
CATALOG_PATH = DATA_DIR / "catalog.jsonl"
STATE_PATH = DATA_DIR / "state.json"
VIDEO_DIR = DATA_DIR / "videos"
SETTINGS_PATH = DATA_DIR / "settings.json"


class AppSettings(BaseModel):
    download_dir: str = ""
    folder_mode: Literal["flat", "date", "category", "date_category"] = "flat"
    workers: int = Field(2, ge=1, le=8)
    fragments: int = Field(4, ge=1, le=16)
    ui_refresh_seconds: int = Field(3, ge=1, le=60)


class SettingsStore:
    def __init__(self) -> None:
        self.lock = Lock()
        self.value = AppSettings(
            download_dir=os.getenv("VIEWKEY_DEFAULT_DOWNLOAD_DIR", ""),
            workers=int(os.getenv("VIEWKEY_WORKERS", "2")),
            fragments=int(os.getenv("VIEWKEY_FRAGMENTS", "4")),
        )
        self.load()

    def load(self) -> None:
        if not SETTINGS_PATH.exists():
            return
        try:
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            self.value = AppSettings(**{**self.value.model_dump(), **raw})
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with self.lock:
            content = json.dumps(self.value.model_dump(), ensure_ascii=False, indent=2)
        temporary = SETTINGS_PATH.with_suffix(".json.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(SETTINGS_PATH)

    def snapshot(self) -> AppSettings:
        with self.lock:
            return AppSettings(**self.value.model_dump())


settings_store = SettingsStore()


def get_video_dir() -> Path:
    configured = settings_store.snapshot().download_dir.strip()
    if not configured:
        return VIDEO_DIR
    path = Path(configured).expanduser()
    return path if path.is_absolute() else DATA_DIR / path


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"
    current: int = 0
    total: int = 0
    message: str = "等待开始"
    error: str = ""
    started_at: float = field(default_factory=time)
    viewkeys: list[str] = field(default_factory=list)
    cancelled: bool = False


class CrawlRequest(BaseModel):
    mode: Literal["category", "author", "url"] = "category"
    category: str = "latest"
    author: str = ""
    url: str = ""
    pages: int = Field(1, ge=0, le=1000)


class DownloadRequest(BaseModel):
    viewkeys: list[str] = Field(min_length=1)
    workers: int = Field(2, ge=1, le=8)
    fragments: int = Field(4, ge=1, le=16)


class RemoveRequest(BaseModel):
    viewkeys: list[str]


class Store:
    def __init__(self) -> None:
        self.lock = Lock()
        self.videos: dict[str, VideoItem] = {}
        self.jobs: dict[str, Job] = {}
        self.download_status: dict[str, dict] = {}
        self.dismissed_downloads: set[str] = set()
        self._load()

    def _load(self) -> None:
        if CATALOG_PATH.exists():
            for line in CATALOG_PATH.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    item = VideoItem.from_json(line)
                    self.videos[item.identity] = item
        if STATE_PATH.exists():
            try:
                raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                self.download_status = raw.get("download_status", {})
                self.dismissed_downloads = set(raw.get("dismissed_downloads", []))
                for status in self.download_status.values():
                    if status.get("state") in {"queued", "downloading"}:
                        status.update(state="failed", error="上次运行被中断，点击重试可继续下载")
            except (json.JSONDecodeError, OSError):
                self.download_status = {}
                self.dismissed_downloads = set()

    def save(self) -> None:
        with self.lock:
            content = "".join(item.to_json() + "\n" for item in self.videos.values())
        self._atomic_write(CATALOG_PATH, content)

    def save_state(self) -> None:
        with self.lock:
            content = json.dumps(
                {
                    "download_status": self.download_status,
                    "dismissed_downloads": sorted(self.dismissed_downloads),
                },
                ensure_ascii=False,
                indent=2,
            )
        self._atomic_write(STATE_PATH, content)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)

    def add(self, item: VideoItem) -> None:
        with self.lock:
            previous = self.videos.get(item.identity)
            if previous:
                for attribute in ("title", "author", "stream_url", "thumbnail_url", "duration", "views"):
                    if not getattr(item, attribute):
                        setattr(item, attribute, getattr(previous, attribute))
                item.sources = list(dict.fromkeys([*previous.sources, *item.sources]))
                item.listing_urls = {**previous.listing_urls, **item.listing_urls}
            self.videos[item.identity] = item

    def replace_source_membership(self, source: str, identities: set[str]) -> None:
        with self.lock:
            for identity, item in list(self.videos.items()):
                if identity in identities or source not in item.sources:
                    continue
                item.sources.remove(source)
                item.listing_urls.pop(source, None)
                if item.source == source:
                    item.source = item.sources[-1] if item.sources else ""
                    item.listing_url = item.listing_urls.get(item.source, "")
                if not item.sources and identity not in self.download_status:
                    self.videos.pop(identity)


store = Store()
app = FastAPI(title="91Fetch")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def disable_local_cache(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


def pagination_info(html: str, current_page: int) -> dict[str, int | bool]:
    soup = BeautifulSoup(html, "html.parser")
    linked_pages: set[int] = set()
    for anchor in soup.select("a[href]"):
        value = parse_qs(urlparse(anchor.get("href", "")).query).get("page", [""])[0]
        if str(value).isdigit():
            linked_pages.add(int(value))
    return {
        "page": current_page,
        "has_previous": current_page > 1,
        "has_next": any(page > current_page for page in linked_pages),
        "last_visible_page": max(linked_pages | {current_page}),
    }


def downloaded_keys() -> set[str]:
    directory = get_video_dir()
    if not directory.exists():
        return set()
    keys: set[str] = set()
    for path in directory.rglob("*"):
        if not path.is_file() or path.name == ".downloaded.txt":
            continue
        if path.suffix.lower() in {".part", ".ytdl", ".temp"}:
            continue
        match = re.search(r"\[([^\]]+)\]\.[^.]+$", path.name)
        if not match:
            continue
        valid, _ = validate_media_file(path)
        if valid:
            keys.add(match.group(1))
    return keys


download_queue_wake = Event()


def queue_download(viewkeys: list[str], workers: int, fragments: int, kind: str = "download") -> Job:
    with store.lock:
        active_keys = {
            key for key, status in store.download_status.items()
            if status.get("state") in {"queued", "downloading"}
        }
        valid = list(dict.fromkeys(key for key in viewkeys if key in store.videos and key not in active_keys))
    if not valid:
        raise HTTPException(409, "所选视频已在下载队列中")
    with store.lock:
        for key in valid:
            store.dismissed_downloads.discard(key)
            store.download_status[key] = {"state": "queued", "percent": 0, "error": ""}
    store.save_state()
    request = DownloadRequest(viewkeys=valid, workers=workers, fragments=fragments)
    job = new_job(kind)
    job.total = len(valid)
    job.viewkeys = valid
    Thread(target=queued_download_worker, args=(job, request), daemon=True).start()
    return job


def queued_download_worker(job: Job, request: DownloadRequest) -> None:
    while not job.cancelled:
        with store.lock:
            running = any(
                other.id != job.id and other.kind == "download"
                and not other.cancelled and other.status == "running"
                for other in store.jobs.values()
            )
            queued = sorted(
                (other for other in store.jobs.values()
                 if other.kind == "download" and not other.cancelled and other.status == "queued"),
                key=lambda other: (other.started_at, other.id),
            )
            if not running and queued and queued[0].id == job.id:
                break
        download_queue_wake.wait(.4)
        download_queue_wake.clear()
    if job.cancelled:
        return
    try:
        download_worker(job, request)
    finally:
        download_queue_wake.set()


def resume_interrupted_downloads() -> None:
    existing = downloaded_keys()
    with store.lock:
        interrupted = [
            key for key, status in store.download_status.items()
            if status.get("state") == "failed"
            and "上次运行被中断" in status.get("error", "")
            and key in store.videos
        ]
        for key in interrupted:
            if key in existing:
                store.download_status[key] = {"state": "completed", "percent": 100, "error": ""}
    pending = [key for key in interrupted if key not in existing]
    store.save_state()
    if pending:
        current = settings_store.snapshot()
        try:
            queue_download(pending, current.workers, current.fragments)
        except HTTPException:
            pass


def new_job(kind: str) -> Job:
    job = Job(id=uuid.uuid4().hex[:12], kind=kind)
    with store.lock:
        store.jobs[job.id] = job
    return job


def crawl_worker(job: Job, request: CrawlRequest) -> None:
    try:
        job.status = "running"
        config = SiteConfig.load(CONFIG_PATH)
        if request.mode == "author":
            start_url = config.author_url.format(author=request.author)
        elif request.mode == "url":
            start_url = request.url
        else:
            start_url = config.category_urls[request.category]
        from urllib.parse import urljoin

        start_url = urljoin(config.base_url, start_url)
        source = request.author or request.category or "custom"
        crawled: set[str] = set()
        with build_client(config, load_cookies(None)) as client:
            crawler = Crawler(client, config)
            for item in crawler.crawl(start_url, request.pages):
                item.add_source(source, start_url)
                store.add(item)
                crawled.add(item.identity)
                job.current += 1
                job.message = f"已采集 {job.current} 个视频"
        store.replace_source_membership(source, crawled)
        store.save()
        job.status = "completed"
        job.message = f"采集完成，共 {job.current} 个视频"
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)
        job.message = "采集失败"


def download_worker(job: Job, request: DownloadRequest) -> None:
    try:
        if job.cancelled:
            return
        job.status = "running"
        config = SiteConfig.load(CONFIG_PATH)
        with store.lock:
            items = [store.videos[key] for key in request.viewkeys if key in store.videos]
        job.total = len(items)
        job.message = "正在刷新视频地址"
        resolved: list[VideoItem] = []
        with build_client(config, load_cookies(None)) as client:
            crawler = Crawler(client, config)
            refreshed: dict[str, VideoItem] = {}
            listing_groups: dict[tuple[str, int], list[VideoItem]] = {}
            from urllib.parse import parse_qs, urljoin, urlparse

            for item in items:
                if job.cancelled:
                    job.status = "failed"
                    job.error = "任务已删除"
                    return
                listing = (
                    item.listing_url
                    or item.listing_urls.get(item.source)
                    or next(iter(item.listing_urls.values()), "")
                    or config.category_urls.get(item.source)
                )
                if not listing:
                    continue
                page_value = parse_qs(urlparse(item.page_url).query).get("page", ["1"])[0]
                page = int(page_value) if str(page_value).isdigit() else 1
                key = (urljoin(config.base_url, listing), page)
                listing_groups.setdefault(key, []).append(item)

            for (listing_url, page), grouped_items in listing_groups.items():
                response = client.get(fresh_listing_url(listing_page_url(listing_url, config.page_param, page, config.first_page)))
                response.raise_for_status()
                fresh_items = parse_listing(response.text, str(response.url), config)
                wanted = {item.identity for item in grouped_items}
                refreshed.update({item.identity: item for item in fresh_items if item.identity in wanted})

            for index, item in enumerate(items, 1):
                fresh_link = refreshed.get(item.identity)
                if fresh_link:
                    item.page_url = fresh_link.page_url
                fresh = crawler.resolve(item)
                thumb_match = re.search(r"/thumb/(?:\d+_)?(\d+)\.jpg", fresh.thumbnail_url, re.I)
                media_match = re.search(r"/mp4\d*/(\d+)\.mp4", fresh.stream_url, re.I)
                if thumb_match and media_match and thumb_match.group(1) != media_match.group(1):
                    raise RuntimeError(f"{fresh.viewkey} 的列表会话已过期，媒体源与封面不匹配，请重新采集后重试")
                resolved.append(fresh)
                store.add(fresh)
                job.message = f"解析视频地址 {index}/{len(items)}"
        store.save()

        completed: set[str] = set()

        def on_progress(item: VideoItem, data: dict) -> None:
            if job.cancelled:
                raise RuntimeError("任务已删除")
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = data.get("downloaded_bytes") or 0
            percent = round(downloaded * 100 / total, 1) if total else 0
            state = "completed" if data.get("status") == "finished" else "downloading"
            with store.lock:
                store.download_status[item.identity] = {
                    "state": state,
                    "percent": percent,
                    "speed": data.get("_speed_str", ""),
                }
            if state == "completed" and item.identity not in completed:
                completed.add(item.identity)
                job.current = len(completed)
                job.message = f"已完成 {job.current}/{job.total}"

        results = BatchDownloader(
            get_video_dir(),
            request.workers,
            request.fragments,
            folder_mode=settings_store.snapshot().folder_mode,
            progress_callback=on_progress,
        ).download(resolved)
        if job.cancelled:
            job.status = "failed"
            job.error = "任务已删除"
            return
        failures = 0
        for item, error in results:
            with store.lock:
                store.download_status[item.identity] = {
                    "state": "failed" if error else "completed",
                    "percent": 100 if not error else 0,
                    "error": error or "",
                }
            failures += bool(error)
        store.save_state()
        job.status = "failed" if failures else "completed"
        job.message = f"下载完成 {len(results) - failures}/{len(results)}"
        job.error = f"{failures} 个任务失败" if failures else ""
    except Exception as exc:
        with store.lock:
            for key in request.viewkeys:
                current = store.download_status.get(key, {})
                if current.get("state") != "completed":
                    store.download_status[key] = {
                        "state": "failed",
                        "percent": current.get("percent", 0),
                        "error": str(exc),
                    }
        store.save_state()
        job.status = "failed"
        job.error = str(exc)
        job.message = "下载失败"


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/catalog")
def catalog(category: str | None = None) -> dict:
    with store.lock:
        items = reversed(list(store.videos.values()))
        videos = [
            asdict(item) for item in items
            if not category or category in item.sources or category == item.source
        ]
        statuses = dict(store.download_status)
    return {"videos": videos, "download_status": statuses}


@app.get("/api/browse")
def browse(category: str = "latest", page: int = 1) -> dict:
    config = SiteConfig.load(CONFIG_PATH)
    if category not in config.category_urls:
        raise HTTPException(400, "未知分类")
    if page < 1 or page > 10000:
        raise HTTPException(400, "页码必须在 1 到 10000 之间")
    if category == "top_day" and page != 1:
        raise HTTPException(400, "每日排行只有一页")
    listing_url = urljoin(config.base_url, config.category_urls[category])
    page_url = fresh_listing_url(listing_page_url(listing_url, config.page_param, page, config.first_page))
    with build_client(config, load_cookies(None)) as client:
        response = client.get(page_url)
        response.raise_for_status()
    items = parse_listing(response.text, str(response.url), config)
    for item in items:
        item.add_source(category, listing_url)
        store.add(item)
    store.save()
    with store.lock:
        statuses = dict(store.download_status)
    return {
        "videos": [asdict(item) for item in items],
        "download_status": statuses,
        "downloaded_keys": sorted(downloaded_keys().intersection(item.identity for item in items)),
        "pagination": pagination_info(response.text, page),
    }


@app.get("/api/downloads")
def downloads() -> dict:
    video_dir = get_video_dir()
    try:
        files = list(video_dir.rglob("*")) if video_dir.exists() else []
    except OSError:
        files = []
    media_files = [
        path for path in files
        if _is_media_candidate(path)
    ]
    files_by_key: dict[str, Path] = {}
    for path in media_files:
        match = re.search(r"\[([^\]]+)\]", path.name)
        if match and (match.group(1) not in files_by_key or _file_mtime(path) > _file_mtime(files_by_key[match.group(1)])):
            files_by_key[match.group(1)] = path
    entries: list[dict] = []
    missing_completed: set[str] = set()
    with store.lock:
        videos = list(store.videos.values())
        statuses = dict(store.download_status)
    for item in reversed(videos):
        if item.identity in store.dismissed_downloads:
            continue
        matched = files_by_key.get(item.identity)
        status = statuses.get(item.identity)
        if not matched and not status:
            continue
        state = status.get("state", "completed") if status else "completed"
        media_valid = False
        validation_error = "本地文件不存在，点击重新下载"
        if matched:
            media_valid, validation_error = validate_media_file(matched)
        if state == "completed" and not media_valid:
            state = "failed"
            status = {
                **(status or {}),
                "state": "failed",
                "percent": 0,
                "error": f"{validation_error}，点击重新下载",
            }
            missing_completed.add(item.identity)
        entries.append(
            {
                "viewkey": item.identity,
                "title": item.title,
                "thumbnail_url": item.thumbnail_url,
                "state": state,
                "percent": status.get("percent", 100 if matched else 0) if status else 100,
                "speed": status.get("speed", "") if status else "",
                "error": status.get("error", "") if status else "",
                "file_name": matched.name if matched else "",
                "file_size": _file_size(matched),
                "modified": _file_mtime(matched),
            }
        )
    if missing_completed:
        with store.lock:
            for entry in entries:
                if entry["viewkey"] in missing_completed:
                    store.download_status[entry["viewkey"]] = {
                        "state": entry["state"],
                        "percent": entry["percent"],
                        "error": entry["error"],
                    }
        store.save_state()
    counts = {name: sum(entry["state"] == name for entry in entries) for name in ("queued", "downloading", "completed", "failed")}
    return {"downloads": entries, "counts": counts, "total_size": sum(entry["file_size"] for entry in entries)}


def _is_media_candidate(path: Path) -> bool:
    try:
        return path.is_file() and path.name != ".downloaded.txt" and path.suffix.lower() not in {".part", ".ytdl", ".temp"}
    except OSError:
        return False


def _file_size(path: Path | None) -> int:
    try:
        return path.stat().st_size if path else 0
    except OSError:
        return 0


def _file_mtime(path: Path | None) -> float:
    try:
        return path.stat().st_mtime if path else 0
    except OSError:
        return 0


@app.post("/api/crawl")
def start_crawl(request: CrawlRequest) -> dict:
    config = SiteConfig.load(CONFIG_PATH)
    if request.mode == "category" and request.category not in config.category_urls:
        raise HTTPException(400, "未知分类")
    if request.mode == "author" and not request.author.strip():
        raise HTTPException(400, "请输入作者 UID")
    if request.mode == "url" and not request.url.strip():
        raise HTTPException(400, "请输入列表 URL")
    with store.lock:
        if any(job.kind == "crawl" and job.status in {"queued", "running"} for job in store.jobs.values()):
            raise HTTPException(409, "已有采集任务正在运行")
    job = new_job("crawl")
    Thread(target=crawl_worker, args=(job, request), daemon=True).start()
    return asdict(job)


@app.post("/api/downloads")
def start_download(request: DownloadRequest) -> dict:
    job = queue_download(request.viewkeys, request.workers, request.fragments)
    return asdict(job)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    with store.lock:
        job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return asdict(job)


@app.post("/api/catalog/remove")
def remove_items(request: RemoveRequest) -> dict:
    with store.lock:
        for key in request.viewkeys:
            store.videos.pop(key, None)
            store.download_status.pop(key, None)
            store.dismissed_downloads.discard(key)
    store.save()
    store.save_state()
    return {"removed": len(request.viewkeys)}


@app.post("/api/downloads/remove")
def remove_downloads(request: RemoveRequest) -> dict:
    with store.lock:
        removed_keys = set(request.viewkeys)
        for job in store.jobs.values():
            if job.kind == "download" and job.status in {"queued", "running"} and removed_keys.intersection(job.viewkeys):
                job.cancelled = True
                job.status = "failed"
                job.error = "任务已删除"
                for key in job.viewkeys:
                    if key not in removed_keys:
                        store.download_status[key] = {"state": "failed", "percent": 0, "error": "同批任务已被删除"}
        for key in request.viewkeys:
            store.download_status.pop(key, None)
            store.dismissed_downloads.add(key)
    store.save_state()
    return {"removed": len(request.viewkeys)}


@app.get("/api/config")
def config_info() -> dict:
    config = SiteConfig.load(CONFIG_PATH)
    return {"base_url": config.base_url, "categories": list(config.category_urls)}


def settings_payload() -> dict:
    current = settings_store.snapshot().model_dump()
    current["download_dir"] = str(get_video_dir().resolve())
    return current


@app.get("/api/settings")
def get_settings() -> dict:
    return settings_payload()


@app.put("/api/settings")
def update_settings(request: AppSettings) -> dict:
    directory = Path(request.download_dir).expanduser()
    if not directory.is_absolute():
        directory = DATA_DIR / directory
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".viewkey-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise HTTPException(400, f"下载目录不可写：{exc}") from exc
    with settings_store.lock:
        settings_store.value = request
    settings_store.save()
    return settings_payload()


def available_port(start: int = 8765) -> int:
    for port in range(start, start + 20):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("没有可用的本地端口")


def main() -> None:
    port = int(os.getenv("VIEWKEY_PORT", "8765"))
    host = os.getenv("VIEWKEY_HOST", "127.0.0.1")
    if host == "127.0.0.1" and not os.getenv("VIEWKEY_PORT"):
        port = available_port()
    url = f"http://127.0.0.1:{port}"
    if os.getenv("VIEWKEY_NO_BROWSER", "0").lower() not in {"1", "true", "yes"}:
        Timer(1.2, lambda: webbrowser.open(url)).start()
    Thread(target=resume_interrupted_downloads, daemon=True, name="viewkey-download-resume").start()
    print(f"91Fetch: {url}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
