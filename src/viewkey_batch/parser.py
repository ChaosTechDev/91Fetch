from __future__ import annotations

from html import unescape
from urllib.parse import parse_qs, unquote, urljoin, urlparse
import json
import re

from bs4 import BeautifulSoup

from .models import SiteConfig, VideoItem


STREAM_PATTERNS = (
    re.compile(r'''(?:file|src|video_url|videoUrl)\s*[:=]\s*["']([^"']+?\.(?:m3u8|mp4)(?:\?[^"']*)?)["']''', re.I),
    re.compile(r'''https?:\\?/\\?/[^"'\s<>]+?\.(?:m3u8|mp4)(?:\?[^"'\s<>]*)?''', re.I),
)
ENCODED_SOURCE_PATTERN = re.compile(r'''strencode2\(\s*["']([^"']+)["']''', re.I)


def viewkey_from_url(url: str) -> str:
    return parse_qs(urlparse(url).query).get("viewkey", [""])[0]


def parse_listing(html: str, page_url: str, config: SiteConfig) -> list[VideoItem]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[VideoItem] = []
    for anchor in soup.select(config.video_link_selector):
        href = anchor.get("href")
        if not href:
            continue
        url = urljoin(page_url, href)
        key = viewkey_from_url(url)
        if not key:
            continue
        container = anchor.find_parent(class_="well") or anchor.parent
        title_node = anchor.select_one(".video-title")
        image = anchor.select_one("img[src]")
        duration_node = anchor.select_one(".duration")
        title = (
            anchor.get("title")
            or (title_node.get_text(" ", strip=True) if title_node else "")
            or (image.get("alt", "") if image else "")
            or anchor.get_text(" ", strip=True)
        )
        details = container.get_text(" ", strip=True) if container else ""
        author_match = re.search(r"(?:From|作者)\s*:\s*(.+?)(?=\s+(?:Views|播放|Favorites|收藏|Comments|评论|$))", details, re.I)
        views_match = re.search(r"(?:Views|播放)\s*:\s*([\d,.]+)", details, re.I)
        candidates.append(
            VideoItem(
                page_url=url,
                viewkey=key,
                title=title,
                author=author_match.group(1).strip() if author_match else "",
                thumbnail_url=urljoin(page_url, image.get("src")) if image and image.get("src") else "",
                duration=duration_node.get_text(strip=True) if duration_node else "",
                views=views_match.group(1) if views_match else "",
            )
        )
    all_tokens = {
        parse_qs(urlparse(item.page_url).query).get("c", [""])[0]
        for item in candidates
    }
    found: list[VideoItem] = []
    seen: set[str] = set()
    for item in candidates:
        token = parse_qs(urlparse(item.page_url).query).get("c", [""])[0]
        # Hidden cards use the current page token prefixed with "a" and may
        # carry another card's key, title, and thumbnail.
        if token.startswith("a") and token[1:] in all_tokens:
            continue
        if item.identity in seen:
            continue
        seen.add(item.identity)
        found.append(item)
    return found


def _clean_stream_url(value: str, page_url: str) -> str:
    value = unescape(value).replace("\\/", "/").replace("\\u0026", "&")
    try:
        if value.startswith('"'):
            value = json.loads(value)
    except json.JSONDecodeError:
        pass
    return urljoin(page_url, unquote(value.strip('"\'')))


def parse_video_page(html: str, page_url: str, item: VideoItem, config: SiteConfig) -> VideoItem:
    soup = BeautifulSoup(html, "html.parser")
    for selector in config.title_selectors:
        node = soup.select_one(selector)
        if node and node.get_text(strip=True):
            item.title = node.get_text(" ", strip=True)
            break
    for selector in config.author_selectors:
        node = soup.select_one(selector)
        if node and node.get_text(strip=True):
            item.author = node.get_text(" ", strip=True)
            break
    # The first plain <source> is shared placeholder media. The per-video
    # signed URL is percent-encoded inside strencode2(...).
    for encoded in ENCODED_SOURCE_PATTERN.findall(html):
        decoded = unquote(unescape(encoded))
        for pattern in STREAM_PATTERNS:
            match = pattern.search(decoded)
            if match:
                value = match.group(1) if match.lastindex else match.group(0)
                item.stream_url = _clean_stream_url(value, page_url)
                return item
    for node in soup.select("video[src], video source[src]"):
        src = node.get("src")
        if src:
            item.stream_url = _clean_stream_url(src, page_url)
            return item
    for pattern in STREAM_PATTERNS:
        match = pattern.search(html)
        if match:
            value = match.group(1) if match.lastindex else match.group(0)
            item.stream_url = _clean_stream_url(value, page_url)
            return item
    return item
