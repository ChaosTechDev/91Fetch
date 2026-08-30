from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from datetime import datetime
from threading import Event, Lock, Thread, Timer
from time import time
from typing import Literal
from urllib.parse import parse_qs, quote, urljoin, urlparse
import base64
import json
import os
import re
import socket
import uuid
import webbrowser

import httpx

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
COOKIES_PATH = DATA_DIR / "cookies.txt"


class AppSettings(BaseModel):
    download_dir: str = ""
    folder_mode: Literal["flat", "date", "category", "date_category"] = "flat"
    prefer_hd: bool = False
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


class CookieRequest(BaseModel):
    content: str


class LoginRequest(BaseModel):
    sid: str
    username: str
    password: str
    captcha: str


# 浏览器内直接登录站点时使用的临时会话（绑定验证码图片），10 分钟过期
_login_sessions: dict[str, tuple[float, httpx.Client]] = {}
_login_sessions_lock = Lock()
LOGIN_SESSION_TTL_SECONDS = 600.0


def _expire_login_sessions() -> None:
    now = time()
    expired = [sid for sid, (created, _) in _login_sessions.items() if now - created > LOGIN_SESSION_TTL_SECONDS]
    for sid in expired:
        _, client = _login_sessions.pop(sid)
        client.close()


def app_cookies() -> dict[str, str]:
    """网页模式使用的会话 Cookie；用户在设置中心保存后所有请求自动携带。"""
    if not COOKIES_PATH.exists():
        return {}
    try:
        return load_cookies(COOKIES_PATH)
    except (OSError, json.JSONDecodeError, KeyError, IndexError):
        return {}


def parse_cookie_text(content: str) -> dict[str, str]:
    """解析粘贴的 Cookie，支持 Netscape 文件内容和 Cookie 请求头两种格式。"""
    content = content.strip()
    if not content:
        return {}
    if content.startswith("# Netscape"):
        temporary = DATA_DIR / ".cookie-import.tmp"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temporary.write_text(content, encoding="utf-8")
        try:
            return load_cookies(temporary)
        finally:
            temporary.unlink(missing_ok=True)
    cookies: dict[str, str] = {}
    for pair in content.replace("\n", ";").split(";"):
        name, _, value = pair.strip().partition("=")
        if name and value:
            cookies[name.strip()] = value.strip()
    return cookies


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
                if not line.strip():
                    continue
                try:
                    item = VideoItem.from_json(line)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
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


_downloaded_keys_cache: dict[str, tuple[float, frozenset[str]]] = {}
_downloaded_keys_lock = Lock()
DOWNLOADED_KEYS_TTL_SECONDS = 5.0


def downloaded_keys() -> set[str]:
    directory = get_video_dir()
    cache_key = str(directory)
    now = time()
    with _downloaded_keys_lock:
        cached = _downloaded_keys_cache.get(cache_key)
        if cached and now - cached[0] < DOWNLOADED_KEYS_TTL_SECONDS:
            return set(cached[1])
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
    with _downloaded_keys_lock:
        _downloaded_keys_cache[cache_key] = (now, frozenset(keys))
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
            # quote 同时完成 URL 转义和花括号转义，避免作者输入破坏 format 模板
            start_url = config.author_url.format(author=quote(request.author, safe=""))
        elif request.mode == "url":
            start_url = request.url
        else:
            start_url = config.category_urls[request.category]
        start_url = urljoin(config.base_url, start_url)
        source = request.author or request.category or "custom"
        crawled: set[str] = set()
        with build_client(config, app_cookies()) as client:
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


def _mark_cancelled(job: Job, request: DownloadRequest) -> None:
    """任务被删除后，把还在排队/下载中的条目标记为失败，避免状态卡死。"""
    with store.lock:
        for key in request.viewkeys:
            current = store.download_status.get(key)
            if current and current.get("state") in {"queued", "downloading"}:
                store.download_status[key] = {
                    "state": "failed",
                    "percent": current.get("percent", 0),
                    "error": "任务已删除",
                }
    store.save_state()
    job.status = "failed"
    job.error = "任务已删除"


def download_worker(job: Job, request: DownloadRequest) -> None:
    try:
        if job.cancelled:
            return
        job.status = "running"
        config = SiteConfig.load(CONFIG_PATH)
        prefer_hd = settings_store.snapshot().prefer_hd
        with store.lock:
            items = [store.videos[key] for key in request.viewkeys if key in store.videos]
        job.total = len(items)
        job.message = "正在刷新视频地址"
        resolved: list[VideoItem] = []
        with build_client(config, app_cookies()) as client:
            crawler = Crawler(client, config)
            refreshed: dict[str, VideoItem] = {}
            listing_groups: dict[tuple[str, int], list[VideoItem]] = {}

            for item in items:
                if job.cancelled:
                    _mark_cancelled(job, request)
                    return
                listing = (
                    item.listing_url
                    or item.listing_urls.get(item.source)
                    or next(iter(item.listing_urls.values()), "")
                    or config.category_urls.get(item.source)
                )
                if not listing:
                    continue
                # 采集时记录的列表页码优先；视频详情页 URL 本身不带 page 参数，
                # 旧清单回退到从 URL 解析，再不行按第 1 页处理
                page = item.listing_page
                if page <= 0:
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
                    # 用副本更新 page_url，避免在锁外修改 store 里的共享对象
                    item = replace(item, page_url=fresh_link.page_url)
                fresh = crawler.resolve(item, prefer_hd=prefer_hd)
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
            cookies=COOKIES_PATH if COOKIES_PATH.exists() else None,
            folder_mode=settings_store.snapshot().folder_mode,
            progress_callback=on_progress,
        ).download(resolved)
        if job.cancelled:
            _mark_cancelled(job, request)
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


def _item_payload(item: VideoItem) -> dict:
    # identity 是 viewkey 为空时的去重主键，前端用它做选择/下载的键
    payload = asdict(item)
    payload["identity"] = item.identity
    return payload


@app.get("/api/catalog")
def catalog(category: str | None = None) -> dict:
    with store.lock:
        items = list(store.videos.values())
        videos = [
            _item_payload(item) for item in reversed(items)
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
    with build_client(config, app_cookies()) as client:
        response = client.get(page_url)
        response.raise_for_status()
    items = parse_listing(response.text, str(response.url), config)
    for item in items:
        item.listing_page = page
        item.add_source(category, listing_url)
        store.add(item)
    store.save()
    with store.lock:
        statuses = dict(store.download_status)
    return {
        "videos": [_item_payload(item) for item in items],
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
        dismissed = set(store.dismissed_downloads)
    for item in reversed(videos):
        if item.identity in dismissed:
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


def save_session_cookies(session: httpx.Client) -> int:
    """把登录会话的 Cookie 以 Netscape 格式落盘，供采集/下载使用。"""
    # session 可能是包着 httpx.Client 的 RateLimitedClient
    jar = getattr(session, "_client", session).cookies.jar
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in jar:
        domain = cookie.domain or ""
        lines.append("\t".join([
            domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            cookie.path or "/",
            "TRUE" if cookie.secure else "FALSE",
            str(int(cookie.expires or 2147483647)),
            cookie.name,
            cookie.value or "",
        ]))
    temporary = COOKIES_PATH.with_suffix(".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(COOKIES_PATH)
    return len(lines) - 1


@app.get("/api/account")
def account_status() -> dict:
    cookies = app_cookies()
    return {"has_cookies": bool(cookies), "cookie_count": len(cookies)}


@app.get("/api/account/captcha")
def account_captcha() -> dict:
    """新建登录会话并返回绑定该会话的验证码图片。"""
    config = SiteConfig.load(CONFIG_PATH)
    # 复用带限速和重试的会话客户端：站点时常瞬断 TLS 连接，裸 httpx 单次请求会直接失败
    session = build_client(config, {})
    try:
        session.get(urljoin(config.base_url, "/login.php"))
        image = session.get(urljoin(config.base_url, "/captcha.php"))
        image.raise_for_status()
    except (httpx.HTTPError, RuntimeError):
        session.close()
        raise HTTPException(502, "获取验证码失败，请稍后重试")
    sid = uuid.uuid4().hex[:12]
    with _login_sessions_lock:
        _expire_login_sessions()
        _login_sessions[sid] = (time(), session)
    mime = image.headers.get("content-type", "image/png").split(";")[0]
    return {"sid": sid, "image": f"data:{mime};base64,{base64.b64encode(image.content).decode()}"}


@app.post("/api/account/login")
def account_login(request: LoginRequest) -> dict:
    with _login_sessions_lock:
        entry = _login_sessions.pop(request.sid, None)
    if entry is None:
        raise HTTPException(400, "验证码会话已过期，请刷新验证码后重试")
    session = entry[1]
    config = SiteConfig.load(CONFIG_PATH)
    fingerprint = uuid.uuid4().hex
    try:
        response = session.post(
            urljoin(config.base_url, "/login.php"),
            data={
                "username": request.username,
                "password": request.password,
                "fingerprint": fingerprint,
                "fingerprint2": fingerprint,
                "captcha_input": request.captcha,
                "action_login": "Log In",
                "submit": "提交",
            },
            headers={"Referer": urljoin(config.base_url, "/login.php")},
        )
    except (httpx.HTTPError, RuntimeError):
        session.close()
        return {"ok": False, "message": "网络请求失败，请重试"}
    html = response.text
    lowered = html.lower()
    # 登录会话一次性使用，无论成败都释放连接；Cookie 已在会话对象里，关闭客户端不影响读取
    session.close()
    if "logout" in lowered or "退出" in html:
        count = save_session_cookies(session)
        return {"ok": True, "message": "登录成功", "cookie_count": count}
    if "验证码" in html or "captcha" in lowered:
        message = "登录失败：验证码错误"
    elif "密码" in html or "password" in lowered:
        message = "登录失败：用户名或密码错误"
    else:
        message = "登录失败，请检查账号信息后重试"
    return {"ok": False, "message": message}


@app.post("/api/account/cookies")
def save_cookies(request: CookieRequest) -> dict:
    cookies = parse_cookie_text(request.content)
    if not cookies:
        raise HTTPException(400, "无法解析 Cookie，请粘贴 Cookie 请求头或 Netscape 文件内容")
    # 统一转成 Netscape 格式落盘，解析器和 yt-dlp 都能直接使用
    config = SiteConfig.load(CONFIG_PATH)
    host = urlparse(config.base_url).netloc.split(":")[0]
    domain = host if host.startswith(".") else f".{host}"
    lines = ["# Netscape HTTP Cookie File"]
    for name, value in cookies.items():
        lines.append("\t".join([domain, "TRUE", "/", "FALSE", "2147483647", name, value]))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = COOKIES_PATH.with_suffix(".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(COOKIES_PATH)
    return {"ok": True, "cookie_count": len(cookies)}


@app.delete("/api/account/cookies")
def clear_cookies() -> dict:
    COOKIES_PATH.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/account/verify")
def verify_login() -> dict:
    config = SiteConfig.load(CONFIG_PATH)
    cookies = app_cookies()
    if not cookies:
        return {"logged_in": False, "has_cookies": False, "cookie_count": 0, "message": "尚未登录"}
    with build_client(config, cookies) as client:
        response = client.get(urljoin(config.base_url, "/"))
        response.raise_for_status()
    html = response.text
    logged_in = "logout" in html.lower() or "退出" in html
    message = "已登录" if logged_in else "Cookie 无效或已过期，请重新登录"
    return {"logged_in": logged_in, "has_cookies": True, "cookie_count": len(cookies), "message": message}


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
