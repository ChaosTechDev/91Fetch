from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import logging

from .http import RateLimitedClient
from .models import SiteConfig, VideoItem
from .parser import parse_listing, parse_video_page


log = logging.getLogger(__name__)


def with_page(url: str, param: str, page: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query[param] = str(page)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class Crawler:
    def __init__(self, client: RateLimitedClient, config: SiteConfig):
        self.client = client
        self.config = config

    def crawl(self, start_url: str, max_pages: int = 0) -> Iterator[VideoItem]:
        seen: set[str] = set()
        page = self.config.first_page
        while not max_pages or page < self.config.first_page + max_pages:
            url = with_page(start_url, self.config.page_param, page)
            log.info("抓取列表页 %s", url)
            response = self.client.get(url)
            response.raise_for_status()
            items = parse_listing(response.text, str(response.url), self.config)
            new_items = [item for item in items if item.identity not in seen]
            if not new_items:
                log.info("第 %d 页没有新视频，停止翻页", page)
                break
            for item in new_items:
                seen.add(item.identity)
                yield item
            page += 1

    def resolve(self, item: VideoItem) -> VideoItem:
        response = self.client.get(item.page_url)
        response.raise_for_status()
        return parse_video_page(response.text, str(response.url), item, self.config)


def append_manifest(path: Path, item: VideoItem) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(item.to_json() + "\n")
