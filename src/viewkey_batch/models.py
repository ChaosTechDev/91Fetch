from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from urllib.parse import urlparse
import hashlib
import json
import logging
import re


log = logging.getLogger(__name__)

# Windows 保留设备名不能直接作为文件名使用
RESERVED_WINDOWS_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
MAX_FILENAME_TITLE = 80


@dataclass(slots=True)
class VideoItem:
    page_url: str
    viewkey: str
    title: str = ""
    author: str = ""
    stream_url: str = ""
    thumbnail_url: str = ""
    duration: str = ""
    views: str = ""
    source: str = ""
    listing_url: str = ""
    listing_page: int = 0
    sources: list[str] = field(default_factory=list)
    listing_urls: dict[str, str] = field(default_factory=dict)

    @property
    def identity(self) -> str:
        return self.viewkey or hashlib.sha256(self.page_url.encode()).hexdigest()[:16]

    @property
    def filename(self) -> str:
        title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", self.title).strip(" ._")
        title = title[:MAX_FILENAME_TITLE].strip(" ._")
        if not title:
            title = "video"
        # Windows 会拒绝 CON、NUL 等保留名组成的文件路径
        if title.upper() in RESERVED_WINDOWS_NAMES:
            title = f"_{title}"
        return f"{title} [{self.identity}]"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    def add_source(self, source: str, listing_url: str = "") -> None:
        if source and source not in self.sources:
            self.sources.append(source)
        if source and listing_url:
            self.listing_urls[source] = listing_url
        if source:
            self.source = source
        if listing_url:
            self.listing_url = listing_url

    @classmethod
    def from_json(cls, line: str) -> "VideoItem":
        raw = json.loads(line)
        source = raw.get("source", "")
        listing_url = raw.get("listing_url", "")
        if not raw.get("sources") and source:
            raw["sources"] = [source]
        if not raw.get("listing_urls") and source and listing_url:
            raw["listing_urls"] = {source: listing_url}
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in raw.items() if key in known})


@dataclass(slots=True)
class SiteConfig:
    base_url: str
    category_urls: dict[str, str]
    author_url: str
    page_param: str = "page"
    first_page: int = 1
    video_link_selector: str = 'a[href*="viewkey="]'
    title_selectors: tuple[str, ...] = ("h1", "h2.title", "title")
    author_selectors: tuple[str, ...] = ('.author a', 'a[href*="UID="]', 'a[href*="author="]')
    timeout: float = 30.0
    request_interval: float = 1.5
    request_jitter: float = 1.0
    max_retries: int = 5
    backoff_base: float = 2.0
    stop_on_challenge: bool = True

    @classmethod
    def load(cls, path: Path) -> "SiteConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["title_selectors"] = tuple(raw.get("title_selectors", ("h1", "h2.title", "title")))
        raw["author_selectors"] = tuple(
            raw.get("author_selectors", ('.author a', 'a[href*="UID="]', 'a[href*="author="]'))
        )
        known = {item.name for item in fields(cls)}
        unknown = [key for key in raw if key not in known]
        for key in unknown:
            log.warning("site.json 中存在已废弃或未知的配置项 %r，已忽略", key)
        return cls(**{key: value for key, value in raw.items() if key in known})

    @property
    def host(self) -> str:
        return urlparse(self.base_url).netloc
