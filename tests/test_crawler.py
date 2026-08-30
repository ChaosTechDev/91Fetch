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
        return_value=httpx.Response(
            200,
            text='<html><body><video src="https://cdn.example.test/mp4hd/999.mp4"></video></body></html>',
        )
    )
    with RateLimitedClient(fast_config(), {}) as client:
        item = Crawler(client, fast_config()).resolve(
            VideoItem("https://example.test/view_video.php?viewkey=abc", "abc"), prefer_hd=True
        )
    assert item.stream_url.endswith("999.mp4")
    assert hd_route.call_count == 1


@respx.mock
def test_resolve_prefer_hd_falls_back_when_hd_page_has_no_stream():
    # 站点对无 VIP 会话返回 200 的高清页，但页面里没有视频源
    respx.get("https://example.test/view_video_hd.php?viewkey=abc").mock(
        return_value=httpx.Response(200, text="<html><head><title>hd shell</title></head><body><h1>空页面</h1></body></html>")
    )
    normal_route = respx.get("https://example.test/view_video.php?viewkey=abc").mock(
        return_value=httpx.Response(
            200,
            text='<html><body><video src="https://cdn.example.test/mp43/123.mp4"></video></body></html>',
        )
    )
    with RateLimitedClient(fast_config(), {}) as client:
        item = Crawler(client, fast_config()).resolve(
            VideoItem("https://example.test/view_video.php?viewkey=abc", "abc"), prefer_hd=True
        )
    assert item.stream_url.endswith("123.mp4")
    assert normal_route.call_count == 1


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
