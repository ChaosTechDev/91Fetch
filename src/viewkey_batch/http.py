from __future__ import annotations

from pathlib import Path
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.parse import urljoin
import json
import logging
import random
import time

import httpx

from .models import SiteConfig


DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}
CHALLENGE_MARKERS = (
    "cf-chl-",
    "captcha",
    "challenge-platform",
    "verify you are human",
    "人机验证",
)
log = logging.getLogger(__name__)


def load_cookies(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    content = path.read_text(encoding="utf-8")
    if content.lstrip().startswith("# Netscape HTTP Cookie File"):
        cookies: dict[str, str] = {}
        for line in content.splitlines():
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) >= 7:
                cookies[fields[5]] = fields[6]
        return cookies
    raw = json.loads(content)
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    return {str(item["name"]): str(item["value"]) for item in raw}


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class RateLimitedClient:
    """Session client with host-friendly pacing and bounded retry behavior."""

    def __init__(self, config: SiteConfig, cookies: dict[str, str], warmup: bool = False):
        self.config = config
        self._last_request = 0.0
        self._client = httpx.Client(
            headers=DEFAULT_HEADERS,
            cookies=cookies,
            timeout=httpx.Timeout(config.timeout),
            follow_redirects=True,
            http2=True,
        )
        self._warmup = warmup
        self._warmed = False

    def __enter__(self) -> "RateLimitedClient":
        if self._warmup and not self._warmed:
            response = self.get(urljoin(self.config.base_url, f"/index.php?_vkb={time.time_ns()}"))
            response.raise_for_status()
            self._warmed = True
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _pace(self) -> None:
        target = self.config.request_interval + random.uniform(0, self.config.request_jitter)
        delay = target - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)

    def get(self, url: str) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            self._pace()
            try:
                response = self._client.get(url)
                self._last_request = time.monotonic()
            except httpx.TransportError as exc:
                last_error = exc
                response = None

            if response is not None and response.status_code in {403, 503} and self.config.stop_on_challenge:
                body_start = response.text[:200_000].lower()
                if any(marker in body_start for marker in CHALLENGE_MARKERS):
                    raise RuntimeError("站点返回了人机验证页，已停止任务；请在浏览器完成验证后导出 Cookie 再继续。")

            if response is not None and response.status_code not in RETRYABLE_STATUSES:
                return response

            if attempt >= self.config.max_retries:
                if response is not None:
                    response.raise_for_status()
                assert last_error is not None
                raise last_error

            server_delay = _retry_after(response) if response is not None else None
            delay = server_delay if server_delay is not None else self.config.backoff_base * (2**attempt)
            delay += random.uniform(0, self.config.request_jitter)
            status = response.status_code if response is not None else type(last_error).__name__
            log.warning("请求受限/失败 (%s)，%.1f 秒后重试 %d/%d", status, delay, attempt + 1, self.config.max_retries)
            time.sleep(delay)

        raise RuntimeError("unreachable")


def build_client(config: SiteConfig, cookies: dict[str, str]) -> RateLimitedClient:
    return RateLimitedClient(config, cookies, warmup=True)
