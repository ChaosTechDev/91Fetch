from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from viewkey_batch.http import RateLimitedClient, _retry_after, load_cookies
from viewkey_batch.models import SiteConfig


def response_with_retry_after(value: str) -> httpx.Response:
    return httpx.Response(429, headers={"Retry-After": value})


def test_retry_after_seconds():
    assert _retry_after(response_with_retry_after("12")) == 12


def test_retry_after_http_date():
    future = datetime.now(timezone.utc) + timedelta(seconds=20)
    delay = _retry_after(response_with_retry_after(format_datetime(future)))
    assert delay is not None and 18 <= delay <= 20


def test_load_netscape_cookies(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\n.example.test\tTRUE\t/\tFALSE\t0\tsession\tTOKEN\n",
        encoding="utf-8",
    )
    assert load_cookies(path) == {"session": "TOKEN"}


def fast_config(**overrides):
    values = {
        "base_url": "https://example.test",
        "category_urls": {},
        "author_url": "/author/{author}",
        "request_interval": 0,
        "request_jitter": 0,
        "backoff_base": 0,
        "max_retries": 2,
    }
    values.update(overrides)
    return SiteConfig(**values)


@respx.mock
def test_rate_limited_client_retries_429():
    route = respx.get("https://example.test/list").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, text="ok")]
    )
    with RateLimitedClient(fast_config(), {}) as client:
        assert client.get("https://example.test/list").text == "ok"
    assert route.call_count == 2


@respx.mock
def test_rate_limited_client_stops_on_challenge():
    route = respx.get("https://example.test/list").mock(
        return_value=httpx.Response(503, text='<div id="cf-chl-widget">Captcha</div>')
    )
    with RateLimitedClient(fast_config(), {}) as client:
        with pytest.raises(RuntimeError, match="人机验证"):
            client.get("https://example.test/list")
    assert route.call_count == 1
