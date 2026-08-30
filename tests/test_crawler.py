import httpx
import respx

from viewkey_batch.crawler import Crawler, to_hd_url
from viewkey_batch.http import RateLimitedClient
from viewkey_batch.models import SiteConfig, VideoItem


def fast_config() -> SiteConfig:
    return SiteConfig(
        base_url="https://example.test",
        category_urls={},
        author_url="/author/{author}",
        request_interval=0,
        request_jitter=0,
        backoff_base=0,
        max_retries=1,
    )


def test_to_hd_url_rewrites_video_page():
    assert to_hd_url("https://example.test/view_video.php?viewkey=abc") == (
        "https://example.test/view_video_hd.php?viewkey=abc"
    )


def test_to_hd_url_keeps_non_video_pages():
    assert to_hd_url("https://example.test/v.php?category=latest") == "https://example.test/v.php?category=latest"


@respx.mock
def test_resolve_prefer_hd_uses_hd_page():
    hd_route = respx.get("https://example.test/view_video_hd.php?viewkey=abc").mock(
        return_value=httpx.Response(200, text="<h1>高清标题</h1>")
    )
    with RateLimitedClient(fast_config(), {}) as client:
        item = Crawler(client, fast_config()).resolve(
            VideoItem("https://example.test/view_video.php?viewkey=abc", "abc"), prefer_hd=True
        )
    assert item.title == "高清标题"
    assert hd_route.call_count == 1


@respx.mock
def test_resolve_prefer_hd_falls_back_to_normal_page():
    respx.get("https://example.test/view_video_hd.php?viewkey=abc").mock(
        return_value=httpx.Response(404, text="not found")
    )
    normal_route = respx.get("https://example.test/view_video.php?viewkey=abc").mock(
        return_value=httpx.Response(200, text="<h1>普通标题</h1>")
    )
    with RateLimitedClient(fast_config(), {}) as client:
        item = Crawler(client, fast_config()).resolve(
            VideoItem("https://example.test/view_video.php?viewkey=abc", "abc"), prefer_hd=True
        )
    assert item.title == "普通标题"
    assert normal_route.call_count == 1
