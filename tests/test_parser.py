from viewkey_batch.crawler import fresh_listing_url, listing_page_url, with_page
from viewkey_batch.models import SiteConfig, VideoItem
from viewkey_batch.parser import parse_listing, parse_video_page


CONFIG = SiteConfig(
    base_url="https://example.test",
    category_urls={"latest": "/v.php?category=rf"},
    author_url="/uvideos.php?UID={author}",
)


def test_listing_extracts_and_deduplicates_viewkeys():
    html = """
    <a href="/view_video.php?viewkey=abc" title="One"></a>
    <a href="view_video.php?viewkey=abc">duplicate</a>
    <a href="/view_video.php?viewkey=xyz">Two</a>
    """
    items = parse_listing(html, "https://example.test/v.php?page=1", CONFIG)
    assert [(item.viewkey, item.title) for item in items] == [("abc", "One"), ("xyz", "Two")]


def test_listing_extracts_hd_video_links():
    html = '<a href="/view_video_hd.php?viewkey=hd123" title="HD video"></a>'
    items = parse_listing(html, "https://example.test/v.php?category=hd", CONFIG)
    assert [(item.viewkey, item.title) for item in items] == [("hd123", "HD video")]


def test_listing_extracts_card_metadata():
    html = """
    <div class="well">
      <a href="/view_video.php?viewkey=abc">
        <img src="https://cdn.test/thumb.jpg">
        <span class="duration">00:04:12</span>
        <span class="video-title">Demo title</span>
      </a>
      <span>From:</span> Alice <span>Views:</span> 12,345 <span>Favorites:</span> 5
    </div>
    """
    item = parse_listing(html, "https://example.test/v.php", CONFIG)[0]
    assert item.title == "Demo title"
    assert item.thumbnail_url == "https://cdn.test/thumb.jpg"
    assert item.duration == "00:04:12"
    assert item.author == "Alice"
    assert item.views == "12,345"


def test_listing_rejects_prefixed_decoy_duplicate():
    html = """
    <div class="well"><a href="/view_video.php?viewkey=abc&c=atoken"><img src="wrong.jpg"><span class="video-title">Wrong</span></a></div>
    <div class="well"><a href="/view_video.php?viewkey=abc&c=token"><img src="right.jpg"><span class="video-title">Right</span></a></div>
    """
    item = parse_listing(html, "https://example.test/v.php", CONFIG)[0]
    assert item.title == "Right"
    assert item.thumbnail_url == "https://example.test/right.jpg"
    assert "c=token" in item.page_url


def test_listing_rejects_page_level_decoy_with_different_viewkey():
    html = """
    <div class="well"><a href="/view_video.php?viewkey=wrong&c=atoken"><span class="video-title">Wrong</span></a></div>
    <div class="well"><a href="/view_video.php?viewkey=real&c=token"><span class="video-title">Right</span></a></div>
    """
    items = parse_listing(html, "https://example.test/v.php", CONFIG)
    assert [(item.viewkey, item.title) for item in items] == [("real", "Right")]


def test_video_page_extracts_escaped_hls_url():
    html = '''<h1>Demo</h1><script>player({file: "https:\\/\\/cdn.test\\/a.m3u8?token=x"})</script>'''
    item = parse_video_page(html, "https://example.test/view_video.php?viewkey=abc", VideoItem("u", "abc"), CONFIG)
    assert item.title == "Demo"
    assert item.stream_url == "https://cdn.test/a.m3u8?token=x"


def test_video_page_prefers_encoded_per_video_source():
    html = """
    <video><source src="https://cdn.test/shared-placeholder.mp4"></video>
    <script>document.write(strencode2("%3Csource%20src%3D%27https%3A%2F%2Fcdn.test%2Freal-123.mp4%3Ftoken%3Dabc%27%3E"));</script>
    """
    item = parse_video_page(html, "https://example.test/watch", VideoItem("u", "abc"), CONFIG)
    assert item.stream_url == "https://cdn.test/real-123.mp4?token=abc"


def test_page_parameter_preserves_existing_query():
    assert with_page("https://x.test/v.php?category=hot", "page", 3) == "https://x.test/v.php?category=hot&page=3"


def test_first_listing_page_preserves_category_and_gets_cache_buster():
    first = listing_page_url("https://x.test/v.php?category=mr&viewtype=basic", "page", 1)
    fresh = fresh_listing_url(first)
    assert "category=mr" in fresh
    assert "viewtype=basic" in fresh
    assert "page=" not in fresh
    assert "_vkb=" in fresh
