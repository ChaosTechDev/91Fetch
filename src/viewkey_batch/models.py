from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse
import hashlib
import json
import re


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
    sources: list[str] = field(default_factory=list)
    listing_urls: dict[str, str] = field(default_factory=dict)

    @property
    def identity(self) -> str:
        return self.viewkey or hashlib.sha256(self.page_url.encode()).hexdigest()[:16]

    @property
    def filename(self) -> str:
        title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", self.title).strip(" ._")
        return f"{title or 'video'} [{self.identity}]"

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
        return cls(**raw)


@dataclass(slots=True)
class SiteConfig:
    base_url: str
    category_urls: dict[str, str]
    author_url: str
    page_param: str = "page"
    first_page: int = 1
    video_link_selector: str = 'a[href*="viewkey="]'
    next_page_selector: str = 'a[rel="next"], a.next, a:contains("下一页")'
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
        return cls(**raw)

    @property
    def host(self) -> str:
        return urlparse(self.base_url).netloc
