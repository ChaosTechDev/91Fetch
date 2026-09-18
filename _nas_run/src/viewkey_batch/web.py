from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from datetime import datetime, timedelta
from threading import Event, Lock, Thread, Timer
from time import time
from typing import Literal
from urllib.parse import parse_qs, urljoin, urlparse
import json
import sqlite3
import os
import re
import socket
import base64
import binascii
import hashlib
import hmac
import secrets
import uuid
import webbrowser
import unicodedata
import copy

from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn
import httpx
import logging

logger = logging.getLogger(__name__)

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
WRITE_LOCK = Lock()
COOKIES_PATH = os.getenv("VIEWKEY_COOKIES_FILE", "").strip()
AUTH_PATH = DATA_DIR / "auth.json"
INVENTORY_PATH = DATA_DIR / "inventory.sqlite3"
INVENTORY_REFRESH_SECONDS = 300
AUTH_ENABLED = os.getenv("VIEWKEY_AUTH_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
SESSION_COOKIE = "viewkey_session"
SESSION_TTL = 7 * 24 * 60 * 60
# Jobs are runtime history, not durable download state. Keep a bounded history
# so frequent scheduled runs cannot retain every request and its viewkey list.
MAX_JOB_HISTORY = max(20, int(os.getenv("VIEWKEY_MAX_JOB_HISTORY", "100")))
# 目录条目上限：定时采集长期运行会让 catalog 无限增长，写盘时淘汰最旧条目
CATALOG_MAX_ENTRIES = max(500, int(os.getenv("VIEWKEY_CATALOG_MAX", "3000")))


class AppSettings(BaseModel):
    download_dir: str = ""
    folder_mode: Literal["flat", "date", "category", "date_category"] = "flat"
    workers: int = Field(2, ge=1, le=8)
    fragments: int = Field(4, ge=1, le=16)
    filter_downloaded: bool = True
    auto_download_enabled: bool = False
    schedule_mode: Literal["interval", "daily"] = "interval"
    interval_minutes: int = Field(60, ge=5, le=10080)
    daily_time: str = "03:00"
    categories: list[str] = Field(default_factory=lambda: ["latest"])
    pages_per_category: int = Field(1, ge=1, le=100)
    ui_refresh_seconds: int = Field(3, ge=1, le=60)
    inventory_refresh_enabled: bool = True
    inventory_schedule_mode: Literal["interval", "daily"] = "interval"
    inventory_interval_minutes: int = Field(60, ge=5, le=10080)
    inventory_daily_time: str = "04:00"


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
            if "categories" in raw:
                raw["categories"] = ["top_month" if value == "top_week" else value for value in raw["categories"]]
            self.value = AppSettings(**{**self.value.model_dump(), **raw})
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with self.lock:
            content = json.dumps(self.value.model_dump(), ensure_ascii=False, indent=2)
        with WRITE_LOCK:
            temporary = SETTINGS_PATH.with_name(f".{SETTINGS_PATH.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(SETTINGS_PATH)

    def snapshot(self) -> AppSettings:
        with self.lock:
            return AppSettings(**self.value.model_dump())


settings_store = SettingsStore()


class AuthStore:
    def __init__(self) -> None:
        self.lock = Lock()
        self.username = os.getenv("VIEWKEY_ADMIN_USER", "admin").strip() or "admin"
        self.salt = ""
        self.password_hash = ""
        self.secret = os.getenv("VIEWKEY_SESSION_SECRET", "").strip() or secrets.token_urlsafe(32)
        self.load()

    @staticmethod
    def _hash(password: str, salt: str) -> str:
        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 240_000).hex()

    def load(self) -> None:
        try:
            raw = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
            self.username = str(raw.get("username") or self.username)
            self.salt = str(raw.get("salt") or "")
            self.password_hash = str(raw.get("password_hash") or "")
            self.secret = str(raw.get("secret") or self.secret)
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        if not self.salt or not self.password_hash:
            self.salt = secrets.token_hex(16)
            initial = os.getenv("VIEWKEY_ADMIN_PASSWORD", "").strip()
            if AUTH_ENABLED and not initial:
                raise RuntimeError("启用登录时必须设置 VIEWKEY_ADMIN_PASSWORD")
            initial = initial or secrets.token_urlsafe(24)
            self.password_hash = self._hash(initial, self.salt)
            self.save()

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"username": self.username, "salt": self.salt, "password_hash": self.password_hash, "secret": self.secret}
        temporary = AUTH_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(AUTH_PATH)

    def verify(self, username: str, password: str) -> bool:
        with self.lock:
            return hmac.compare_digest(username, self.username) and hmac.compare_digest(self._hash(password, self.salt), self.password_hash)

    def update(self, username: str, password: str) -> None:
        with self.lock:
            self.username = username.strip()
            self.salt = secrets.token_hex(16)
            self.password_hash = self._hash(password, self.salt)
            self.save()

    def make_session(self) -> str:
        expiry = int(time()) + SESSION_TTL
        payload = f"{self.username}:{expiry}"
        signature = hmac.new(self.secret.encode(), payload.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(f"{payload}:{signature.hex()}".encode()).decode().rstrip("=")

    def session_user(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
            username, expiry, supplied = raw.rsplit(":", 2)
            payload = f"{username}:{expiry}"
            expected = hmac.new(self.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
            if int(expiry) < int(time()) or not hmac.compare_digest(supplied, expected):
                return None
            return username if hmac.compare_digest(username, self.username) else None
        except (ValueError, UnicodeError, binascii.Error):
            return None


auth_store = AuthStore()


def get_video_dir() -> Path:
    configured = settings_store.snapshot().download_dir.strip()
    if not configured:
        return VIDEO_DIR
    path = Path(configured).expanduser()
    return path if path.is_absolute() else DATA_DIR / path


def web_cookies() -> dict[str, str]:
    """Load optional local Netscape/JSON cookies without exposing contents."""
    return load_cookies(Path(COOKIES_PATH) if COOKIES_PATH else None)


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


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class PasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    username: str = Field(min_length=1, max_length=80)
    new_password: str = Field(min_length=8, max_length=256)


class Store:
    def __init__(self) -> None:
        self.lock = Lock()
        self.videos: dict[str, VideoItem] = {}
        self.jobs: dict[str, Job] = {}
        self.download_status: dict[str, dict] = {}
        self.completed_keys: set[str] = set()
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
                self.completed_keys = set(raw.get("completed_keys", []))
                self.completed_keys.update(
                    key for key, status in self.download_status.items() if status.get("state") == "completed"
                )
                self.dismissed_downloads = set(raw.get("dismissed_downloads", []))
                for status in self.download_status.values():
                    if status.get("state") in {"queued", "downloading"}:
                        status.update(state="failed", error="上次运行被中断，点击重试可继续下载")
            except (json.JSONDecodeError, OSError):
                self.download_status = {}
                self.completed_keys = set()
                self.dismissed_downloads = set()

    def save(self) -> None:
        with self.lock:
            self._prune_catalog_locked()
            content = "".join(item.to_json() + "\n" for item in self.videos.values())
        with WRITE_LOCK:
            self._atomic_write(CATALOG_PATH, content)

    def save_state(self) -> None:
        with self.lock:
            content = json.dumps(
                {
                    "download_status": self.download_status,
                    "completed_keys": sorted(self.completed_keys),
                    "dismissed_downloads": sorted(self.dismissed_downloads),
                },
                ensure_ascii=False,
                indent=2,
            )
        with WRITE_LOCK:
            self._atomic_write(STATE_PATH, content)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)

    def _prune_catalog_locked(self) -> None:
        """定时采集会让目录无限膨胀；写盘前淘汰最旧的、与进行中任务无关的条目。

        已下载的标记保留在 completed_keys 里，条目被重新采集时"已下载"徽章仍然生效。
        """
        overflow = len(self.videos) - CATALOG_MAX_ENTRIES
        if overflow <= 0:
            return
        active = {
            identity for identity, status in self.download_status.items()
            if status.get("state") in {"queued", "downloading"}
        }
        for identity in list(self.videos):
            if overflow <= 0:
                break
            if identity in active:
                continue
            del self.videos[identity]
            self.download_status.pop(identity, None)
            self.dismissed_downloads.discard(identity)
            overflow -= 1

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
    if AUTH_ENABLED and request.url.path.startswith("/api/") and not request.url.path.startswith("/api/auth/"):
        if not auth_store.session_user(request.cookies.get(SESSION_COOKIE)):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "请先登录"}, status_code=401)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/api/auth/status")
def auth_status(request: Request) -> dict:
    user = auth_store.session_user(request.cookies.get(SESSION_COOKIE)) if AUTH_ENABLED else "local"
    return {"enabled": AUTH_ENABLED, "authenticated": bool(user), "username": user or ""}


@app.post("/api/auth/login")
def auth_login(payload: LoginRequest):
    if not AUTH_ENABLED:
        return {"authenticated": True, "username": "local"}
    if not auth_store.verify(payload.username, payload.password):
        raise HTTPException(401, "账号或密码错误")
    from fastapi.responses import JSONResponse
    response = JSONResponse({"authenticated": True, "username": auth_store.username})
    response.set_cookie(SESSION_COOKIE, auth_store.make_session(), max_age=SESSION_TTL, httponly=True, samesite="lax")
    return response


@app.post("/api/auth/logout")
def auth_logout():
    from fastapi.responses import JSONResponse
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.put("/api/auth/password")
def auth_password(request: Request, payload: PasswordRequest):
    if AUTH_ENABLED and not auth_store.session_user(request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(401, "请先登录")
    if AUTH_ENABLED and not auth_store.verify(auth_store.username, payload.current_password):
        raise HTTPException(400, "当前密码错误")
    auth_store.update(payload.username, payload.new_password)
    return {"username": auth_store.username, "message": "账号密码已更新，请重新登录"}


@app.on_event("startup")
async def start_background_scheduler() -> None:
    start_scheduler()
    start_inventory_scheduler()
    Thread(target=resume_interrupted_downloads, daemon=True, name="viewkey-download-resume").start()


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


scheduler_stop = Event()
scheduler_wake = Event()
scheduler_lock = Lock()
scheduler_runtime: dict[str, object] = {
    "running": False,
    "last_run": "",
    "next_run": "",
    "message": "定时任务未启用",
}
inventory_scheduler_stop = Event()
inventory_scheduler_wake = Event()
inventory_scheduler_lock = Lock()
inventory_scheduler_runtime: dict[str, object] = {
    "running": False,
    "last_run": "",
    "next_run": "",
    "message": "库存定时刷新未启用",
    "file_count": 0,
    "duration": 0.0,
}
download_queue_wake = Event()


def next_schedule(settings: AppSettings, now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    if settings.schedule_mode == "daily":
        try:
            hour, minute = (int(part) for part in settings.daily_time.split(":", 1))
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except (ValueError, TypeError):
            candidate = now + timedelta(minutes=settings.interval_minutes)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    return now + timedelta(minutes=settings.interval_minutes)


def next_inventory_schedule(settings: AppSettings, now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    if settings.inventory_schedule_mode == "daily":
        try:
            hour, minute = (int(part) for part in settings.inventory_daily_time.split(":", 1))
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except (ValueError, TypeError):
            candidate = now + timedelta(minutes=settings.inventory_interval_minutes)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    return now + timedelta(minutes=settings.inventory_interval_minutes)


def downloaded_keys() -> set[str]:
    inventory.ensure_ready()
    keys: set[str] = set()
    title_files: dict[str, Path] = {}
    for row in inventory.records():
        if row["identity"]:
            keys.add(row["identity"])
        if row["title_key"]:
            title_files[row["title_key"]] = Path(row["path"])
    with store.lock:
        # A completed marker survives task deletion only while its local media
        # still exists; the file scan also discovers videos imported manually.
        for item in store.videos.values():
            if canonical_title(item.title) in title_files:
                keys.add(item.identity)
        new_keys = keys - store.completed_keys
        store.completed_keys.update(new_keys)
    if new_keys:
        store.save_state()
    return keys


def canonical_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    value = re.sub(r"\s*\[[^\]]+\]$", "", value)
    value = re.sub(r"\s*-\s*91porn(?:\s*\[[^\]]+\])?$", "", value, flags=re.I)
    return re.sub(r"[^\w]+", "", value, flags=re.UNICODE)


def canonical_file_title(name: str) -> str:
    return canonical_title(Path(name).stem)


def scan_media_files(root: Path | None = None) -> list[Path]:
    """Return finished media files directly from disk for inventory recovery."""
    root = root or get_video_dir()
    if not root.exists():
        return []
    files: list[Path] = []
    try:
        candidates = root.rglob("*")
        for path in candidates:
            try:
                if not path.is_file() or path.name == ".downloaded.txt":
                    continue
                if path.suffix.lower() in {".part", ".ytdl", ".temp"}:
                    continue
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size >= 256 * 1024:
                files.append(path)
    except OSError:
        return files
    return files


class LocalInventory:
    def __init__(self) -> None:
        self.lock = Lock()
        self.last_refresh = 0.0
        self.last_root = ""
        self.last_duration = 0.0
        self.last_count = 0
        self._records_cache: list[dict] | None = None

    def _connect(self) -> sqlite3.Connection:
        INVENTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(INVENTORY_PATH)
        connection.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, identity TEXT, title_key TEXT, size INTEGER NOT NULL, mtime REAL NOT NULL)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_files_identity ON files(identity)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_files_title ON files(title_key)")
        return connection

    def refresh(self, force: bool = False) -> None:
        started = time()
        now = time()
        with self.lock:
            root = get_video_dir()
            if not force and str(root) == self.last_root and now - self.last_refresh < INVENTORY_REFRESH_SECONDS:
                return
            seen: list[tuple[str, str | None, str, int, float]] = []
            for path in scan_media_files(root):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                match = re.search(r"\[([^\]]+)\]\.[^.]+$", path.name)
                seen.append((str(path), match.group(1) if match else None, canonical_file_title(path.name), stat.st_size, stat.st_mtime))
            connection = self._connect()
            try:
                connection.execute("CREATE TEMP TABLE seen_paths (path TEXT PRIMARY KEY)")
                connection.executemany("INSERT INTO seen_paths(path) VALUES (?)", ((row[0],) for row in seen))
                connection.executemany("INSERT INTO files(path, identity, title_key, size, mtime) VALUES (?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET identity=excluded.identity, title_key=excluded.title_key, size=excluded.size, mtime=excluded.mtime", seen)
                connection.execute("DELETE FROM files WHERE path NOT IN (SELECT path FROM seen_paths)")
                connection.commit()
            finally:
                connection.close()
            self.last_refresh = now
            self.last_root = str(root)
            self.last_duration = round(time() - started, 3)
            self.last_count = len(seen)
            self._records_cache = None

    def ensure_ready(self) -> None:
        if str(get_video_dir()) != self.last_root or not self.last_refresh:
            self.refresh(force=True)

    def records(self) -> list[dict]:
        # /api/downloads 每秒轮询一次，刷新间隔内直接复用缓存，避免每秒全表扫描
        with self.lock:
            if self._records_cache is not None:
                return [dict(row) for row in self._records_cache]
        connection = self._connect()
        try:
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute("SELECT path, identity, title_key, size, mtime FROM files")]
        finally:
            connection.close()
        with self.lock:
            self._records_cache = rows
        return [dict(row) for row in rows]


inventory = LocalInventory()
# A single empty/incomplete NAS scan must not flip a completed task to failed.
# Require several consecutive observations before declaring a file deleted.
missing_file_observations: dict[str, int] = {}
MISSING_FILE_CONFIRMATIONS = 3


def queue_download(viewkeys: list[str], workers: int, fragments: int, kind: str = "download") -> Job:
    # 第一次加锁：验证并过滤有效键
    with store.lock:
        active_keys = {
            key for key, status in store.download_status.items()
            if status.get("state") in {"queued", "downloading"}
        }
        filtered = [
            key for key in viewkeys if key in store.videos and key not in active_keys
        ]
        valid = list(dict.fromkeys(filtered))  # 去重并保持顺序
    if not valid:
        raise HTTPException(409, "所选视频已在下载队列中")
    
    # 第二次加锁：设置下载状态 - 避免锁外修改共享状态
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
                other.id != job.id
                and other.kind == "download"
                and not other.cancelled
                and other.status == "running"
                for other in store.jobs.values()
            )
            queued = sorted(
                (
                    other for other in store.jobs.values()
                    if other.kind == "download"
                    and not other.cancelled
                    and other.status == "queued"
                ),
                key=lambda other: (other.started_at, other.id),
            )
            if not running and queued and queued[0].id == job.id:
                job.status = "running"
                job.message = "正在准备下载"
                break
        download_queue_wake.wait(0.8)
        download_queue_wake.clear()
    if job.cancelled:
        return
    try:
        download_worker(job, request)
    finally:
        download_queue_wake.set()


def resume_interrupted_downloads() -> None:
    inventory.ensure_ready()
    existing = downloaded_keys()
    with store.lock:
        interrupted = [
            key for key, status in store.download_status.items()
            if status.get("state") == "failed"
            and "上次运行被中断" in status.get("error", "")
            and key in store.videos
        ]
        completed = set(interrupted) & existing
        for key in completed:
            store.download_status[key] = {"state": "completed", "percent": 100, "error": ""}
            store.completed_keys.add(key)
    pending = [key for key in interrupted if key not in completed]
    store.save_state()
    if pending:
        current = settings_store.snapshot()
        queue_download(pending, current.workers, current.fragments)


def scheduled_refresh(force: bool = False) -> str:
    config = SiteConfig.load(CONFIG_PATH)
    current = settings_store.snapshot()
    if not current.auto_download_enabled and not force:
        return "定时任务未启用"
    with store.lock:
        busy = any(job.kind == "download" and job.status in {"queued", "running"} for job in store.jobs.values())
    if busy:
        return "已有下载任务运行，本次刷新跳过"
    
    found: dict[str, VideoItem] = {}
    categories_processed = 0
    pages_processed = 0
    
    with build_client(config, web_cookies()) as client:
        for category in current.categories:
            if category not in config.category_urls:
                continue
            try:
                listing_url = urljoin(config.base_url, config.category_urls[category])
                page_count = 1 if category == "top_day" else current.pages_per_category
                for page in range(1, page_count + 1):
                    try:
                        response = client.get(fresh_listing_url(listing_page_url(listing_url, config.page_param, page)))
                        response.raise_for_status()
                        for item in parse_listing(response.text, str(response.url), config):
                            item.add_source(category, listing_url)
                            store.add(item)
                            found[item.identity] = item
                        pages_processed += 1
                    except httpx.TransportError as transport_exc:
                        logger.warning("页面 %d 传输错误：%s", page, transport_exc)
                        # 对于传输错误，尝试重试一次
                        try:
                            response = client.get(fresh_listing_url(listing_page_url(listing_url, config.page_param, page)))
                            response.raise_for_status()
                            for item in parse_listing(response.text, str(response.url), config):
                                item.add_source(category, listing_url)
                                store.add(item)
                                found[item.identity] = item
                            pages_processed += 1
                        except Exception as retry_exc:
                            logger.error("页面 %d 重试失败：%s", page, retry_exc)
                            continue
            except Exception as cat_exc:
                logger.error("分类 %s 处理失败：%s", category, cat_exc)
                continue
            categories_processed += 1
    
    store.save()
    
    if categories_processed == 0:
        raise RuntimeError("所有分类都处理失败，请检查网络连接和 Cookie 有效性")
    
    existing_files = downloaded_keys() if current.filter_downloaded else set()
    with store.lock:
        active = {
            key for key, status in store.download_status.items()
            if status.get("state") in {"queued", "downloading"}
        }
        dismissed = set(store.dismissed_downloads)
    candidates = [key for key in found if key not in existing_files and key not in active and key not in dismissed]
    
    if not candidates:
        return f"刷新完成，发现 {len(found)} 个视频，没有新的下载任务"
    
    job = queue_download(candidates, current.workers, current.fragments)
    return f"刷新完成，发现 {len(found)} 个视频 (遍历{categories_processed}个分类,{pages_processed}页)，已加入 {job.total} 个下载任务"


def scheduler_loop() -> None:
    while not scheduler_stop.is_set():
        current = settings_store.snapshot()
        with scheduler_lock:
            next_at = scheduler_runtime.get("next_at")
            if current.auto_download_enabled and not isinstance(next_at, datetime):
                next_at = next_schedule(current)
                scheduler_runtime["next_at"] = next_at
                scheduler_runtime["next_run"] = next_at.isoformat(timespec="seconds")
                scheduler_runtime["message"] = "定时任务已启用"
            if not current.auto_download_enabled:
                scheduler_runtime["next_at"] = None
                scheduler_runtime["next_run"] = ""
                scheduler_runtime["message"] = "定时任务未启用"
        if current.auto_download_enabled and isinstance(next_at, datetime) and datetime.now() >= next_at:
            with scheduler_lock:
                if scheduler_runtime["running"]:
                    scheduler_wake.wait(5)
                    scheduler_wake.clear()
                    continue
                scheduler_runtime["running"] = True
                scheduler_runtime["last_run"] = datetime.now().isoformat(timespec="seconds")
                scheduler_runtime["message"] = "正在刷新分类并筛选新视频"
                scheduler_runtime["next_at"] = next_schedule(current)
                scheduler_runtime["next_run"] = scheduler_runtime["next_at"].isoformat(timespec="seconds")
            try:
                message = scheduled_refresh()
            except Exception as exc:
                import traceback
                error_msg = f"定时任务失败：{type(exc).__name__}: {exc}"
                logger.error(error_msg)
                message = f"定时任务失败：{exc}"
                # 记录详细错误日志
                logger.debug(traceback.format_exc())
                # 重置下次运行时间为当前周期的下一个点
                with scheduler_lock:
                    scheduler_runtime["next_at"] = next_schedule(current)
                    scheduler_runtime["next_run"] = scheduler_runtime["next_at"].isoformat(timespec="seconds")
            with scheduler_lock:
                scheduler_runtime["running"] = False
                scheduler_runtime["message"] = message
            continue
        wait_seconds = 5
        if current.auto_download_enabled and isinstance(next_at, datetime):
            wait_seconds = max(1, min(5, int((next_at - datetime.now()).total_seconds())))
        scheduler_wake.wait(wait_seconds)
        scheduler_wake.clear()


def start_scheduler() -> None:
    with scheduler_lock:
        if scheduler_runtime.get("thread") and scheduler_runtime["thread"].is_alive():
            return
        scheduler_runtime["thread"] = Thread(target=scheduler_loop, daemon=True, name="viewkey-scheduler")
        scheduler_runtime["thread"].start()


def refresh_inventory_now() -> None:
    with inventory_scheduler_lock:
        if inventory_scheduler_runtime["running"]:
            return
        inventory_scheduler_runtime["running"] = True
        inventory_scheduler_runtime["message"] = "正在扫描本地视频目录"
    try:
        inventory.refresh(force=True)
        message = f"库存刷新完成，共识别 {inventory.last_count} 个本地文件"
    except Exception as exc:
        message = f"库存刷新失败：{exc}"
    with inventory_scheduler_lock:
        inventory_scheduler_runtime.update(
            running=False,
            last_run=datetime.now().isoformat(timespec="seconds"),
            message=message,
            file_count=inventory.last_count,
            duration=inventory.last_duration,
        )


def inventory_scheduler_loop() -> None:
    initial_scan = True
    while not inventory_scheduler_stop.is_set():
        current = settings_store.snapshot()
        with inventory_scheduler_lock:
            next_at = inventory_scheduler_runtime.get("next_at")
            if initial_scan:
                next_at = datetime.now()
                initial_scan = False
            elif current.inventory_refresh_enabled and not isinstance(next_at, datetime):
                next_at = next_inventory_schedule(current)
            if current.inventory_refresh_enabled:
                inventory_scheduler_runtime["next_at"] = next_at
                inventory_scheduler_runtime["next_run"] = next_at.isoformat(timespec="seconds") if isinstance(next_at, datetime) else ""
            else:
                inventory_scheduler_runtime["next_at"] = None
                inventory_scheduler_runtime["next_run"] = ""
                inventory_scheduler_runtime["message"] = "库存定时刷新未启用"
        if isinstance(next_at, datetime) and datetime.now() >= next_at:
            refresh_inventory_now()
            current = settings_store.snapshot()
            with inventory_scheduler_lock:
                if current.inventory_refresh_enabled:
                    next_at = next_inventory_schedule(current)
                    inventory_scheduler_runtime["next_at"] = next_at
                    inventory_scheduler_runtime["next_run"] = next_at.isoformat(timespec="seconds")
                else:
                    inventory_scheduler_runtime["next_at"] = None
                    inventory_scheduler_runtime["next_run"] = ""
            continue
        inventory_scheduler_wake.wait(5)
        inventory_scheduler_wake.clear()


def start_inventory_scheduler() -> None:
    with inventory_scheduler_lock:
        thread = inventory_scheduler_runtime.get("thread")
        if thread and thread.is_alive():
            return
        inventory_scheduler_runtime["thread"] = Thread(
            target=inventory_scheduler_loop, daemon=True, name="viewkey-inventory-scheduler"
        )
        inventory_scheduler_runtime["thread"].start()


def new_job(kind: str) -> Job:
    job = Job(id=uuid.uuid4().hex[:12], kind=kind)
    with store.lock:
        _prune_job_history_locked()
        store.jobs[job.id] = job
    return job


def _prune_job_history_locked() -> None:
    """Release terminal job payloads and retain only a small runtime history."""
    terminal = [job for job in store.jobs.values() if job.status in {"completed", "failed", "cancelled"}]
    for job in terminal:
        # viewkeys are only needed while a job can be cancelled. Clearing them
        # after completion releases potentially large request lists promptly.
        if job.viewkeys:
            job.viewkeys = []
    excess = len(terminal) - MAX_JOB_HISTORY
    if excess <= 0:
        return
    for job in sorted(terminal, key=lambda value: (value.started_at, value.id))[:excess]:
        store.jobs.pop(job.id, None)


def crawl_worker(job: Job, request: CrawlRequest) -> None:
    try:
        job.status = "running"
        config = SiteConfig.load(CONFIG_PATH)
        from urllib.parse import urljoin, quote

        if request.mode == "author":
            start_url = config.author_url.format(author=quote(request.author, safe=""))
        elif request.mode == "url":
            start_url = request.url
        else:
            start_url = config.category_urls[request.category]
        start_url = urljoin(config.base_url, start_url)
        source = request.author or request.category or "custom"
        crawled: set[str] = set()
        with build_client(config, web_cookies()) as client:
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
    finally:
        with store.lock:
            if job.cancelled and job.status in {"queued", "running"}:
                job.status = "failed"
                job.error = job.error or "任务已删除"
            _prune_job_history_locked()


def download_worker(job: Job, request: DownloadRequest) -> None:
    try:
        # 检查取消状态 - 在任务启动前检查
        with store.lock:
            if job.cancelled:
                return
        job.status = "running"
        config = SiteConfig.load(CONFIG_PATH)
        with store.lock:
            active_keys = set(job.viewkeys) if job.viewkeys else set(request.viewkeys)
            items = [copy.deepcopy(store.videos[key]) for key in request.viewkeys if key in active_keys and key in store.videos]
        job.total = len(items)
        job.message = "正在刷新视频地址"
        resolved: list[VideoItem] = []
        from urllib.parse import parse_qs, urlparse

        with build_client(config, web_cookies()) as client:
            crawler = Crawler(client, config)
            refreshed: dict[str, VideoItem] = {}
            listing_groups: dict[tuple[str, int], list[VideoItem]] = {}

            for item in items:
                # 定期检查取消状态（但不在循环中频繁加锁）
                cancelled = False
                with store.lock:
                    cancelled = job.cancelled
                if cancelled:
                    with store.lock:
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
                response = client.get(fresh_listing_url(listing_page_url(listing_url, config.page_param, page)))
                response.raise_for_status()
                fresh_items = parse_listing(response.text, str(response.url), config)
                wanted = {item.identity for item in grouped_items}
                refreshed.update({item.identity: item for item in fresh_items if item.identity in wanted})

            for index, item in enumerate(items, 1):
                with store.lock:
                    if item.identity not in job.viewkeys:
                        continue
                fresh_link = refreshed.get(item.identity)
                if fresh_link:
                    item.page_url = fresh_link.page_url
                try:
                    prefer_hd = settings_store.snapshot().prefer_hd
                    fresh = crawler.resolve(item, prefer_hd=prefer_hd)
                    thumb_match = re.search(r"/thumb/(?:\d+_)?(\d+)\.jpg", fresh.thumbnail_url, re.I)
                    media_match = re.search(r"/mp4\d*/(\d+)\.mp4", fresh.stream_url, re.I)
                    if thumb_match and media_match and thumb_match.group(1) != media_match.group(1):
                        raise RuntimeError("列表会话已过期，媒体源与封面不匹配，请重新采集后重试")
                except Exception as exc:
                    with store.lock:
                        store.download_status[item.identity] = {"state": "failed", "percent": 0, "error": str(exc)}
                    continue
                resolved.append(fresh)
                store.add(fresh)
                job.message = f"解析视频地址 {index}/{len(items)}"
        store.save()
        if not resolved:
            store.save_state()
            job.status = "failed"
            job.error = "批次中的视频均无法解析"
            job.message = "没有可下载的视频"
            return

        completed: set[str] = set()

        def on_progress(item: VideoItem, data: dict) -> None:
            # 检查取消状态需加锁
            cancelled = False
            with store.lock:
                cancelled = job.cancelled
                if cancelled:
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
                if state == "completed":
                    store.completed_keys.add(item.identity)
            
            if state == "completed" and item.identity not in completed:
                completed.add(item.identity)
                # job.current 更新也需在锁内完成
                with store.lock:
                    job.current = len(completed)
                    job.message = f"已完成 {job.current}/{job.total}"

        results = BatchDownloader(
            get_video_dir(),
            request.workers,
            request.fragments,
            folder_mode=settings_store.snapshot().folder_mode,
            progress_callback=on_progress,
        ).download([item for item in resolved if item.identity in job.viewkeys])
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
                if not error:
                    store.completed_keys.add(item.identity)
            failures += bool(error)
        inventory.refresh(force=True)
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
    finally:
        with store.lock:
            _prune_job_history_locked()


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
    page_url = fresh_listing_url(listing_page_url(listing_url, config.page_param, page))
    with build_client(config, web_cookies()) as client:
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


# 文件校验结果缓存：键为 (路径, 大小, 修改时间)，文件内容没变就不重复读盘。
# /api/downloads 每秒轮询会对所有已完成文件逐个校验，无缓存时是纯粹的重复开销。
_media_validation_cache: dict[tuple[str, int, float], tuple[bool, str]] = {}


def validate_media_file_cached(path: Path) -> tuple[bool, str]:
    try:
        stat = path.stat()
    except OSError as exc:
        return False, f"无法校验媒体文件：{exc}"
    key = (str(path), stat.st_size, stat.st_mtime)
    cached = _media_validation_cache.get(key)
    if cached is None:
        cached = validate_media_file(path)
        if len(_media_validation_cache) > 4096:
            _media_validation_cache.clear()
        _media_validation_cache[key] = cached
    return cached


@app.get("/api/downloads")
def downloads() -> dict:
    try:
        inventory.ensure_ready()
        media_files = [Path(row["path"]) for row in inventory.records()]
    except (OSError, sqlite3.Error):
        # A locked or damaged inventory must not hide finished files or break
        # the download manager; the filesystem is the source of truth here.
        media_files = scan_media_files()
    files_by_key: dict[str, Path] = {}
    files_by_title: dict[str, Path] = {}
    for path in media_files:
        match = re.search(r"\[([^\]]+)\]", path.name)
        if match and (match.group(1) not in files_by_key or _file_mtime(path) > _file_mtime(files_by_key[match.group(1)])):
            files_by_key[match.group(1)] = path
        title = canonical_file_title(path.name)
        if title and (title not in files_by_title or _file_mtime(path) > _file_mtime(files_by_title[title])):
            files_by_title[title] = path
    entries: list[dict] = []
    missing_completed: set[str] = set()
    recovered_completed: set[str] = set()
    with store.lock:
        videos = list(store.videos.values())
        statuses = dict(store.download_status)
    # A download may finish between inventory refreshes, or the SQLite refresh
    # can be interrupted by a concurrent poll. Recover failed rows from disk
    # before declaring the local file missing.
    if any(status.get("state") == "failed" for status in statuses.values()):
        known = {str(path) for path in media_files}
        media_files.extend(path for path in scan_media_files() if str(path) not in known)
        files_by_key.clear()
        files_by_title.clear()
        for path in media_files:
            match = re.search(r"\[([^\]]+)\]", path.name)
            if match and (match.group(1) not in files_by_key or _file_mtime(path) > _file_mtime(files_by_key[match.group(1)])):
                files_by_key[match.group(1)] = path
            title = canonical_file_title(path.name)
            if title and (title not in files_by_title or _file_mtime(path) > _file_mtime(files_by_title[title])):
                files_by_title[title] = path
    for item in reversed(videos):
        if item.identity in store.dismissed_downloads:
            continue
        matched = files_by_key.get(item.identity) or files_by_title.get(canonical_title(item.title))
        status = statuses.get(item.identity)
        if not matched and not status:
            continue
        state = status.get("state", "completed") if status else "completed"
        media_valid = False
        validation_error = "本地文件不存在，点击重新下载"
        if matched:
            media_valid, validation_error = validate_media_file_cached(matched)
        if matched and media_valid:
            missing_file_observations.pop(item.identity, None)
            if state != "completed" or (status and status.get("error")):
                recovered_completed.add(item.identity)
            state = "completed"
            status = {**(status or {}), "state": "completed", "percent": 100, "error": ""}
        elif state == "completed":
            observations = missing_file_observations.get(item.identity, 0) + 1
            missing_file_observations[item.identity] = observations
            if observations >= MISSING_FILE_CONFIRMATIONS:
                state = "failed"
                status = {
                    **(status or {}),
                    "state": "failed",
                    "percent": 0,
                    "error": validation_error or "本地文件不存在，点击重新下载",
                }
                missing_completed.add(item.identity)
            else:
                # Keep the durable completed state while the inventory catches
                # up; this prevents the UI from oscillating between two tabs.
                status = {**(status or {}), "state": "completed", "percent": 100, "error": ""}
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
    if missing_completed or recovered_completed:
        with store.lock:
            for entry in entries:
                if entry["viewkey"] in missing_completed or entry["viewkey"] in recovered_completed:
                    store.download_status[entry["viewkey"]] = {
                        "state": entry["state"],
                        "percent": entry["percent"],
                        "error": entry["error"],
                    }
                    if entry["state"] == "completed":
                        store.completed_keys.add(entry["viewkey"])
        store.save_state()
    counts = {name: sum(entry["state"] == name for entry in entries) for name in ("queued", "downloading", "completed", "failed")}
    return {"downloads": entries, "counts": counts, "total_size": sum(entry["file_size"] for entry in entries)}


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
                job.viewkeys = [key for key in job.viewkeys if key not in removed_keys]
                if not job.viewkeys:
                    job.cancelled = True
                    job.status = "failed"
                    job.error = "任务已删除"
        for key in request.viewkeys:
            store.download_status.pop(key, None)
            store.dismissed_downloads.add(key)
    store.save_state()
    download_queue_wake.set()
    return {"removed": len(request.viewkeys)}


@app.get("/api/config")
def config_info() -> dict:
    config = SiteConfig.load(CONFIG_PATH)
    return {"base_url": config.base_url, "categories": list(config.category_urls)}


def settings_payload() -> dict:
    config = SiteConfig.load(CONFIG_PATH)
    current = settings_store.snapshot().model_dump()
    current["download_dir"] = str(get_video_dir().resolve())
    current["available_categories"] = list(config.category_urls)
    with scheduler_lock:
        current["scheduler"] = {
            "running": scheduler_runtime.get("running", False),
            "last_run": scheduler_runtime.get("last_run", ""),
            "next_run": scheduler_runtime.get("next_run", ""),
            "message": scheduler_runtime.get("message", ""),
        }
    with inventory_scheduler_lock:
        current["inventory_scheduler"] = {
            "running": inventory_scheduler_runtime.get("running", False),
            "last_run": inventory_scheduler_runtime.get("last_run", ""),
            "next_run": inventory_scheduler_runtime.get("next_run", ""),
            "message": inventory_scheduler_runtime.get("message", ""),
            "file_count": inventory_scheduler_runtime.get("file_count", inventory.last_count),
            "duration": inventory_scheduler_runtime.get("duration", inventory.last_duration),
        }
    return current


@app.get("/api/settings")
def get_settings() -> dict:
    return settings_payload()


@app.put("/api/settings")
def update_settings(request: AppSettings) -> dict:
    config = SiteConfig.load(CONFIG_PATH)
    invalid = sorted(set(request.categories) - set(config.category_urls))
    if invalid:
        raise HTTPException(400, f"未知分类：{', '.join(invalid)}")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", request.daily_time):
        raise HTTPException(400, "每日时间必须是 HH:MM")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", request.inventory_daily_time):
        raise HTTPException(400, "库存每日刷新时间必须是 HH:MM")
    previous_directory = get_video_dir()
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
    with scheduler_lock:
        scheduler_runtime["next_at"] = next_schedule(request) if request.auto_download_enabled else None
        scheduler_runtime["next_run"] = (
            scheduler_runtime["next_at"].isoformat(timespec="seconds")
            if isinstance(scheduler_runtime["next_at"], datetime) else ""
        )
        scheduler_runtime["message"] = "设置已保存"
    scheduler_wake.set()
    with inventory_scheduler_lock:
        inventory_scheduler_runtime["next_at"] = (
            next_inventory_schedule(request) if request.inventory_refresh_enabled else None
        )
        inventory_scheduler_runtime["next_run"] = (
            inventory_scheduler_runtime["next_at"].isoformat(timespec="seconds")
            if isinstance(inventory_scheduler_runtime["next_at"], datetime) else ""
        )
        inventory_scheduler_runtime["message"] = "库存刷新设置已保存"
    inventory_scheduler_wake.set()
    if previous_directory != get_video_dir():
        Thread(target=refresh_inventory_now, daemon=True, name="viewkey-inventory-directory-change").start()
    return settings_payload()


@app.post("/api/settings/run-now")
def run_settings_now() -> dict:
    with scheduler_lock:
        if scheduler_runtime.get("running"):
            raise HTTPException(409, "定时任务正在运行")
        scheduler_runtime["running"] = True
        scheduler_runtime["last_run"] = datetime.now().isoformat(timespec="seconds")
        scheduler_runtime["message"] = "正在立即刷新分类"

    def worker() -> None:
        try:
            message = scheduled_refresh(force=True)
        except Exception as exc:
            message = f"立即任务失败：{exc}"
        with scheduler_lock:
            scheduler_runtime["running"] = False
            scheduler_runtime["message"] = message

    Thread(target=worker, daemon=True, name="viewkey-run-now").start()
    return {"started": True}


@app.post("/api/settings/inventory/run-now")
def run_inventory_now() -> dict:
    with inventory_scheduler_lock:
        if inventory_scheduler_runtime.get("running"):
            raise HTTPException(409, "本地库存正在刷新")
    Thread(target=refresh_inventory_now, daemon=True, name="viewkey-inventory-run-now").start()
    return {"started": True}


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
    print(f"ViewKey Batch: {url}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()

